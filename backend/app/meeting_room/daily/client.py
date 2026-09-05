from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Optional

import aiohttp


class DailyClientError(Exception):
    """Base class for Daily client configuration / availability failures."""


class DailyNotConfiguredError(DailyClientError):
    """``DAILY_API_KEY`` is empty or missing."""


class DailyUnavailableError(DailyClientError):
    """pipecat/daily-python are not installed on this platform."""


_daily_utils_cache: Optional[SimpleNamespace] = None


def _daily_utils() -> SimpleNamespace:
    global _daily_utils_cache
    if _daily_utils_cache is None:
        from pipecat.transports.daily.utils import (
            DailyMeetingTokenParams,
            DailyMeetingTokenProperties,
            DailyRESTHelper,
            DailyRoomParams,
            DailyRoomProperties,
        )

        _daily_utils_cache = SimpleNamespace(
            DailyMeetingTokenParams=DailyMeetingTokenParams,
            DailyMeetingTokenProperties=DailyMeetingTokenProperties,
            DailyRESTHelper=DailyRESTHelper,
            DailyRoomParams=DailyRoomParams,
            DailyRoomProperties=DailyRoomProperties,
        )
    return _daily_utils_cache


class DailyClient:
    def __init__(self, api_key: str, aiohttp_session: aiohttp.ClientSession):
        self._api_key = api_key
        self._http = aiohttp_session
        self._helper: Any = None
        self._utils: Optional[SimpleNamespace] = None

    def _get_helper(self) -> Any:
        if self._helper is not None:
            return self._helper

        if not self._api_key:
            raise DailyNotConfiguredError("DAILY_API_KEY is not configured")

        try:
            self._utils = _daily_utils()
        except ImportError as exc:
            raise DailyUnavailableError(
                "pipecat/daily-python are not installed. They ship Linux-only "
                "wheels, so the Meeting Room is unavailable on this platform."
            ) from exc

        self._helper = self._utils.DailyRESTHelper(
            daily_api_key=self._api_key,
            aiohttp_session=self._http,
        )
        return self._helper

    def ensure_available(self) -> None:
        self._get_helper()

    async def create_room(self, *, room_expiry_seconds: int, max_participants: int) -> Any:
        helper = self._get_helper()
        utils = self._utils
        return await helper.create_room(
            utils.DailyRoomParams(
                privacy="private",
                properties=utils.DailyRoomProperties(
                    exp=time.time() + room_expiry_seconds,
                    start_video_off=True,
                    enable_prejoin_ui=False,
                    max_participants=max_participants,
                ),
            )
        )

    async def get_token(
        self,
        room_url: str,
        *,
        expiry_time: int,
        owner: bool,
        user_name: Optional[str] = None,
    ) -> str:
        helper = self._get_helper()
        params = None
        if not owner:
            utils = self._utils
            params = utils.DailyMeetingTokenParams(
                properties=utils.DailyMeetingTokenProperties(
                    user_name=user_name or "Guest",
                    start_video_off=True,
                )
            )
        return await helper.get_token(
            room_url,
            expiry_time=expiry_time,
            owner=owner,
            params=params,
        )

    def get_name_from_url(self, room_url: str) -> str:
        return self._get_helper().get_name_from_url(room_url)

    async def delete_room(self, room_name: str) -> None:
        await self._get_helper().delete_room_by_name(room_name)
