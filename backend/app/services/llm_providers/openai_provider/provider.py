from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Any, Optional, Sequence

import openai
from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.config import settings as global_settings
from app.services.llm_providers.provider_interface import LLMProvider
from app.services.llm_providers.provider_exceptions import (
    LLMRateLimitError,
    LLMResponseParsingError,
    LLMSchemaValidationError,
    LLMStructuredOutputUnsupportedError,
)
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.json_schema_validation import SchemaSpec, validate_against_schema
from app.services.llm_providers.openai_sdk_errors import translate_openai_sdk_error


class OpenAIProvider(LLMProvider):
    """Talks to OpenAI's own API directly via the Responses API
    (`client.responses.create`) — OpenAI's recommended entry point for new
    integrations, rather than Chat Completions. Deliberately does not mirror
    every OpenRouterProvider behavior: no `temperature` is ever sent (current
    OpenAI guidance for reasoning-tier models is to omit it rather than
    send-then-retry-on-rejection), and `cost` is always `None` since OpenAI's
    own API doesn't report a per-call cost figure the way OpenRouter does.
    Sends a fixed `reasoning.effort` (default `"low"`, from
    `settings.resume_room_question_reasoning_effort`) — OpenAI's own guidance
    recommends `low` for grading/classification/rewrite-shaped tasks like
    this one; the model's own default (`medium`) costs real latency here."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        async_client: Optional[AsyncOpenAI] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        self._settings = settings or global_settings
        self.model = model or self._settings.resume_room_question_model
        self.timeout = timeout or self._settings.llm_request_timeout_seconds
        self.reasoning_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else self._settings.resume_room_question_reasoning_effort
        )

        self._async_client = async_client or AsyncOpenAI(
            api_key=self._settings.openai_api_key,
            timeout=self.timeout,
            max_retries=0,
        )

    @property
    def supports_prompt_caching(self) -> bool:
        """PromptCaching protocol: the first (system) message is marked with
        an explicit `prompt_cache_breakpoint` — see `_build_request_kwargs`."""
        return True

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        options: Optional[LLMRequestOptions] = None,
    ) -> LLMResponse:
        """Send a Responses API request and return a normalized LLMResponse."""
        resolved_options = options or LLMRequestOptions()
        request_kwargs = self._build_request_kwargs(messages, resolved_options)
        raw_response = await self._call_responses_async(**request_kwargs)
        return self._parse_raw_response_to_llm_response(raw_response)

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        schema: SchemaSpec,
        options: Optional[LLMRequestOptions] = None,
        max_repair_retries: Optional[int] = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        """Generate structured JSON output conforming to the given schema.

        Same shape as OpenRouterProvider.generate_json (strict json_schema
        first, one-time downgrade to json_object, then a repair loop up to
        max_repair_retries) — only the request/response translation differs,
        since the Responses API expresses structured output as
        `text.format` rather than `response_format`.
        """
        resolved_options = options or LLMRequestOptions()
        retries_allowed = (
            max_repair_retries
            if max_repair_retries is not None
            else self._settings.max_json_repair_retries
        )

        conversation_messages: list[LLMMessage] = list(messages)
        response_format_mode = "json_schema"
        last_raw_text = ""
        validation_errors: list[str] = []
        repair_attempts_used = 0
        attempt_responses: list[LLMResponse] = []

        while True:
            request_kwargs = self._build_request_kwargs(conversation_messages, resolved_options)
            request_kwargs["text"] = {
                "format": self._build_text_format(schema, response_format_mode)
            }

            try:
                raw_response = await self._call_responses_async(**request_kwargs)
            except LLMStructuredOutputUnsupportedError:
                if response_format_mode == "json_schema":
                    # One-time mode downgrade — does not consume a repair attempt
                    response_format_mode = "json_object"
                    continue
                raise

            llm_response = self._parse_raw_response_to_llm_response(raw_response)
            attempt_responses.append(llm_response)
            last_raw_text = llm_response.text

            parsed_candidate: Optional[dict[str, Any]]
            try:
                parsed_candidate = json.loads(last_raw_text)
                validation_errors = validate_against_schema(parsed_candidate, schema)
            except json.JSONDecodeError as json_error:
                parsed_candidate = None
                validation_errors = [f"Response was not valid JSON: {json_error}"]

            if parsed_candidate is not None and not validation_errors:
                return parsed_candidate, self._merge_attempt_usage(attempt_responses)

            if repair_attempts_used >= retries_allowed:
                raise LLMSchemaValidationError(
                    f"Model output did not satisfy schema {schema.name!r} after "
                    f"{repair_attempts_used} repair attempt(s)",
                    last_raw_text=last_raw_text,
                    validation_errors=validation_errors,
                    last_response=self._merge_attempt_usage(attempt_responses),
                )

            repair_attempts_used += 1
            conversation_messages = conversation_messages + [
                LLMMessage(
                    role="user",
                    content=self._build_repair_prompt(last_raw_text, schema, validation_errors),
                )
            ]

    @staticmethod
    def _build_text_format(schema_spec: SchemaSpec, response_format_mode: str) -> dict[str, Any]:
        if response_format_mode == "json_schema":
            return {
                "type": "json_schema",
                "name": schema_spec.name,
                "schema": schema_spec.json_schema,
                "strict": schema_spec.strict,
            }
        if response_format_mode == "json_object":
            return {"type": "json_object"}
        raise ValueError(f"Unknown response_format mode: {response_format_mode!r}")

    @staticmethod
    def _merge_attempt_usage(attempts: list[LLMResponse]) -> LLMResponse:
        """Collapse a generate_json repair loop's per-attempt LLMResponses into
        one, the same way OpenRouterProvider does: text/model/response_id/
        finish_reason/raw_response come from the last attempt, token fields
        are summed across every attempt since each is a separate billed call.
        `cost` stays `None` — never reported by OpenAI's own API."""
        final_attempt = attempts[-1]
        return replace(
            final_attempt,
            prompt_tokens=sum(attempt.prompt_tokens for attempt in attempts),
            completion_tokens=sum(attempt.completion_tokens for attempt in attempts),
            total_tokens=sum(attempt.total_tokens for attempt in attempts),
            cached_tokens=sum(
                attempt.cached_tokens for attempt in attempts if attempt.cached_tokens is not None
            )
            or None,
            reasoning_tokens=sum(
                attempt.reasoning_tokens
                for attempt in attempts
                if attempt.reasoning_tokens is not None
            )
            or None,
        )

    def _build_request_kwargs(
        self,
        messages: Sequence[LLMMessage],
        options: LLMRequestOptions,
    ) -> dict[str, Any]:
        """Assemble the keyword arguments for a Responses API call.

        The first message, when it's a system/developer prompt, is marked
        with an explicit `prompt_cache_breakpoint` rather than relying on
        implicit caching: for these single-turn calls the user message (the
        dynamic resume/history/coverage payload) changes on every call, so
        implicit caching's one breakpoint — placed at the end of the latest
        eligible message — would land on that ever-changing payload and
        never actually cache the stable system prompt. An explicit
        breakpoint after the system prompt, keyed by a hash of its content,
        caches that stable prefix instead."""
        dict_messages: list[dict[str, Any]] = []
        cache_key_source: Optional[str] = None
        for message_index, message in enumerate(messages):
            if (
                message_index == 0
                and message.role in ("system", "developer")
                and isinstance(message.content, str)
            ):
                dict_messages.append(
                    {
                        "role": message.role,
                        "content": [
                            {
                                "type": "input_text",
                                "text": message.content,
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                )
                cache_key_source = message.content
            else:
                dict_messages.append(self._convert_message_to_dict(message))

        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "input": dict_messages,
            "max_output_tokens": options.max_output_tokens,
            # Interview transcripts/resume data are candidate PII — don't
            # retain responses server-side beyond what's needed to serve them.
            "store": False,
        }
        if cache_key_source is not None:
            request_kwargs["prompt_cache_options"] = {"mode": "explicit"}
            request_kwargs["prompt_cache_key"] = self._prompt_cache_key_for(cache_key_source)
        if self.reasoning_effort:
            request_kwargs["reasoning"] = {"effort": self.reasoning_effort}
        if options.metadata:
            request_kwargs["metadata"] = options.metadata
        return request_kwargs

    @staticmethod
    def _prompt_cache_key_for(system_prompt_text: str) -> str:
        """Derive a stable cache key from the system prompt's own content, so
        each distinct system prompt (per call site) gets its own cache
        lineage without requiring a call site to pass one explicitly."""
        digest = hashlib.sha256(system_prompt_text.encode("utf-8")).hexdigest()[:16]
        return f"question_chain_{digest}"

    def _convert_message_to_dict(self, message: LLMMessage) -> dict[str, Any]:
        return {"role": message.role, "content": message.content}

    async def _call_responses_async(self, **request_kwargs: Any) -> Any:
        """Execute a Responses API call with automatic rate-limit retry —
        the same backoff loop OpenRouterProvider uses, minus the
        temperature-rejection branch (this provider never sends
        `temperature`)."""
        attempt_number = 0

        while True:
            try:
                return await self._async_client.responses.create(**request_kwargs)
            except openai.APIError as openai_sdk_api_error:
                provider_agnostic_error = translate_openai_sdk_error(openai_sdk_api_error)

                if (
                    isinstance(provider_agnostic_error, LLMRateLimitError)
                    and attempt_number < self._settings.max_rate_limit_retries
                ):
                    backoff_delay = provider_agnostic_error.retry_after_seconds
                    if backoff_delay is None:
                        backoff_delay = self._settings.rate_limit_backoff_base_seconds * (
                            2**attempt_number
                        )
                    await asyncio.sleep(backoff_delay)
                    attempt_number += 1
                    continue

                raise provider_agnostic_error from openai_sdk_api_error

    def _extract_text_from_response(self, raw_response: Any) -> str:
        """Extract the text content from a Responses API result. Prefers the
        SDK's `output_text` convenience property; falls back to walking
        `output` message items for a raw/mocked response that doesn't set it."""
        output_text = getattr(raw_response, "output_text", None)
        if output_text:
            return output_text

        output_items = getattr(raw_response, "output", None) or []
        extracted_text_parts: list[str] = []
        for output_item in output_items:
            if getattr(output_item, "type", None) != "message":
                continue
            for content_part in getattr(output_item, "content", None) or []:
                part_text = getattr(content_part, "text", None)
                if part_text:
                    extracted_text_parts.append(part_text)
        if extracted_text_parts:
            return "".join(extracted_text_parts)

        raise LLMResponseParsingError("Response contained no output text")

    def _parse_raw_response_to_llm_response(self, raw_response: Any) -> LLMResponse:
        """Parse a raw Responses API result into a normalized LLMResponse."""
        text = self._extract_text_from_response(raw_response)
        token_usage = getattr(raw_response, "usage", None)

        prompt_tokens = getattr(token_usage, "input_tokens", 0) or 0
        completion_tokens = getattr(token_usage, "output_tokens", 0) or 0
        total_tokens = getattr(token_usage, "total_tokens", None) or (
            prompt_tokens + completion_tokens
        )

        input_tokens_details = getattr(token_usage, "input_tokens_details", None)
        cached_tokens = (
            getattr(input_tokens_details, "cached_tokens", None) if input_tokens_details else None
        )
        output_tokens_details = getattr(token_usage, "output_tokens_details", None)
        reasoning_tokens = (
            getattr(output_tokens_details, "reasoning_tokens", None)
            if output_tokens_details
            else None
        )

        status = getattr(raw_response, "status", None)
        finish_reason = status
        if status == "incomplete":
            incomplete_details = getattr(raw_response, "incomplete_details", None)
            finish_reason = getattr(incomplete_details, "reason", None) or status

        return LLMResponse(
            text=text,
            model=getattr(raw_response, "model", self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=None,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            response_id=getattr(raw_response, "id", None),
            finish_reason=finish_reason,
            raw_response=raw_response,
        )

    def _build_repair_prompt(
        self,
        raw_text: str,
        schema: SchemaSpec,
        validation_errors: list[str],
    ) -> str:
        formatted_validation_errors = (
            "\n".join(f"- {error}" for error in validation_errors)
            or "(no specific errors captured)"
        )
        return (
            "The previous response was supposed to be JSON matching the schema below, "
            "but it did not. Return ONLY corrected JSON — no prose, no markdown fences.\n\n"
            f"Schema (name={schema.name}):\n{json.dumps(schema.json_schema, indent=2)}\n\n"
            f"Validation errors:\n{formatted_validation_errors}\n\n"
            f"Previous (invalid) response:\n{raw_text}"
        )
