from __future__ import annotations

import asyncio
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
    LLMTemperatureUnsupportedError,
)
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.json_schema_validation import (
    SchemaSpec,
    build_response_format,
    validate_against_schema,
)

from .errors import translate_openai_sdk_error


class OpenRouterProvider(LLMProvider):

    provider_name = "openrouter"

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        async_client: Optional[AsyncOpenAI] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self._settings = settings or global_settings
        self.model = model or self._settings.openrouter_default_model
        self.temperature = temperature if temperature is not None else self._settings.default_temperature
        self.timeout = timeout or self._settings.llm_request_timeout_seconds

        self._async_client = async_client or AsyncOpenAI(
            api_key=self._settings.openrouter_api_key,
            base_url=self._settings.openrouter_base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    @property
    def supports_prompt_caching(self) -> bool:
        """PromptCaching protocol: OpenRouter supports provider-side prompt
        caching via explicit cache breakpoints on system messages."""
        return True


    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        options: Optional[LLMRequestOptions] = None,
    ) -> LLMResponse:
        """Send a chat completion request and return a normalized LLMResponse."""
        resolved_options = options or LLMRequestOptions()
        api_formatted_messages, extra_body = self._format_messages_for_api(messages, resolved_options)
        request_kwargs = self._build_request_kwargs(
            api_formatted_messages, resolved_options, extra_body, model=self.model
        )
        raw_response = await self._call_chat_completions_async(**request_kwargs)
        return self._parse_raw_response_to_llm_response(raw_response, model=self.model)


    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        schema: SchemaSpec,
        options: Optional[LLMRequestOptions] = None,
        max_repair_retries: Optional[int] = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        """Generate structured JSON output conforming to the given schema.

        Uses strict json_schema mode first, falling back to json_object mode
        if the model doesn't support structured outputs. Validates the
        response locally and runs a repair loop (up to max_repair_retries)
        if the output doesn't match the schema. Returns the parsed JSON
        alongside an LLMResponse whose usage/cost fields are summed across
        every attempt, since each repair attempt is a separately billed call.
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
            api_formatted_messages, extra_body = self._format_messages_for_api(
                conversation_messages, resolved_options
            )
            request_kwargs = self._build_request_kwargs(
                api_formatted_messages, resolved_options, extra_body, model=self.model
            )
            request_kwargs["response_format"] = build_response_format(
                schema, response_format_mode=response_format_mode
            )

            try:
                raw_response = await self._call_chat_completions_async(**request_kwargs)
            except LLMStructuredOutputUnsupportedError:
                if response_format_mode == "json_schema":
                    # One-time mode downgrade — does not consume a repair attempt
                    response_format_mode = "json_object"
                    continue
                raise

            llm_response = self._parse_raw_response_to_llm_response(raw_response, model=self.model)
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
    def _merge_attempt_usage(attempts: list[LLMResponse]) -> LLMResponse:
        """Collapse a generate_json repair loop's per-attempt LLMResponses into
        one: text/model/response_id/finish_reason/raw_response come from the
        last attempt (the one whose text was actually parsed), while token and
        cost fields are summed across every attempt, since each is a separate
        billed API call."""

        def _sum_optional(values: list[Optional[Any]]) -> Optional[float]:
            present = [value for value in values if value is not None]
            return sum(present) if present else None

        final_attempt = attempts[-1]
        return replace(
            final_attempt,
            prompt_tokens=sum(attempt.prompt_tokens for attempt in attempts),
            completion_tokens=sum(attempt.completion_tokens for attempt in attempts),
            total_tokens=sum(attempt.total_tokens for attempt in attempts),
            cost=_sum_optional([attempt.cost for attempt in attempts]),
            cached_tokens=_sum_optional([attempt.cached_tokens for attempt in attempts]),
            reasoning_tokens=_sum_optional([attempt.reasoning_tokens for attempt in attempts]),
        )


    def _format_messages_for_api(
        self,
        messages: Sequence[LLMMessage],
        options: LLMRequestOptions,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Converts LLMMessage objects to OpenAI-shaped message dicts, and
        attaches the system-prompt cache marker if caching is enabled."""
        api_formatted_messages = [self._convert_message_to_dict(message) for message in messages]

        should_cache_system_prompt = (
            options.cache_system_prompt
            if options.cache_system_prompt is not None
            else self._settings.system_prompt_caching_enabled
        )

        if should_cache_system_prompt:
            for formatted_message_entry in api_formatted_messages:
                if formatted_message_entry["role"] == "system":
                    formatted_message_entry["content"] = [
                        {
                            "type": "text",
                            "text": formatted_message_entry["content"],
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ]
                    break  # only the first system message is marked

        return api_formatted_messages, {}

    def _convert_message_to_dict(self, message: LLMMessage) -> dict[str, Any]:
        """Convert a single LLMMessage to an OpenAI-compatible dict."""
        return {
            "role": message.role,
            "content": message.content,
        }

    def _build_request_kwargs(
        self,
        messages_payload: list[dict[str, Any]],
        options: LLMRequestOptions,
        extra_body: dict[str, Any],
        *,
        model: str,
    ) -> dict[str, Any]:
        """Assemble the keyword arguments for a chat completions API call."""
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages_payload,
            "max_tokens": options.max_output_tokens,
        }

        resolved_temperature = (
            options.temperature if options.temperature is not None else self.temperature
        )
        if resolved_temperature is not None:
            request_kwargs["temperature"] = resolved_temperature

        merged_openrouter_extra_body = dict(extra_body)
        if options.metadata:
            merged_openrouter_extra_body["metadata"] = options.metadata
        if options.models_fallback:
            merged_openrouter_extra_body["models"] = list(options.models_fallback)
        if options.extra_provider_params:
            merged_openrouter_extra_body.update(options.extra_provider_params)

        # Ask OpenRouter to report real per-request cost so
        # _extract_cost_from_usage can read it back.
        merged_openrouter_extra_body.setdefault("usage", {"include": True})

        if merged_openrouter_extra_body:
            request_kwargs["extra_body"] = merged_openrouter_extra_body
        return request_kwargs


    async def _call_chat_completions_async(self, **request_kwargs: Any) -> Any:
        """Execute a chat completions API call with automatic retry logic
        for temperature rejection and rate limiting."""
        attempt_number = 0
        current_request_kwargs = dict(request_kwargs)

        while True:
            try:
                return await self._async_client.chat.completions.create(**current_request_kwargs)
            except openai.APIError as openai_sdk_api_error:
                provider_agnostic_error = translate_openai_sdk_error(openai_sdk_api_error)

                if (
                    isinstance(provider_agnostic_error, LLMTemperatureUnsupportedError)
                    and "temperature" in current_request_kwargs
                ):
                    current_request_kwargs = {
                        key: value
                        for key, value in current_request_kwargs.items()
                        if key != "temperature"
                    }
                    continue

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
        """Extract the text content from an OpenAI chat completion response."""
        response_choices = getattr(raw_response, "choices", None) or []
        if not response_choices:
            raise LLMResponseParsingError("Response contained no choices")

        first_choice_message = getattr(response_choices[0], "message", None)
        message_content = (
            getattr(first_choice_message, "content", None)
            if first_choice_message is not None
            else None
        )
        if message_content is None:
            raise LLMResponseParsingError("Response choice contained no message content")

        if isinstance(message_content, str):
            return message_content
        if isinstance(message_content, list):
            extracted_text_parts = []
            for content_part in message_content:
                if isinstance(content_part, dict):
                    extracted_text_parts.append(content_part.get("text", ""))
                else:
                    extracted_text_parts.append(str(getattr(content_part, "text", "")))
            return "".join(extracted_text_parts)
        return str(message_content)

    def _parse_raw_response_to_llm_response(self, raw_response: Any, *, model: str) -> LLMResponse:
        """Parse a raw OpenAI SDK response into a normalized LLMResponse."""
        text = self._extract_text_from_response(raw_response)
        token_usage = getattr(raw_response, "usage", None)

        prompt_tokens = getattr(token_usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(token_usage, "completion_tokens", 0) or 0
        total_tokens = getattr(token_usage, "total_tokens", None) or (prompt_tokens + completion_tokens)
        cost = self._extract_cost_from_usage(token_usage)
        cached_tokens, reasoning_tokens = self._extract_token_breakdown_from_usage(token_usage)

        response_choices = getattr(raw_response, "choices", None) or []
        finish_reason = getattr(response_choices[0], "finish_reason", None) if response_choices else None

        return LLMResponse(
            text=text,
            model=getattr(raw_response, "model", model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=cost,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            response_id=getattr(raw_response, "id", None),
            finish_reason=finish_reason,
            raw_response=raw_response,
        )

    def _extract_cost_from_usage(self, token_usage: Any) -> Optional[float]:
        """Extract the real cost from OpenRouter's usage response field."""
        if token_usage is None:
            return None

        reported_cost = getattr(token_usage, "cost", None)
        if reported_cost is not None:
            return float(reported_cost)

        sdk_extra_fields = getattr(token_usage, "model_extra", None) or {}
        if isinstance(sdk_extra_fields, dict) and "cost" in sdk_extra_fields:
            try:
                return float(sdk_extra_fields["cost"])
            except (TypeError, ValueError):
                return None
        return None

    def _extract_token_breakdown_from_usage(
        self, token_usage: Any
    ) -> tuple[Optional[int], Optional[int]]:
        """Extract cached_tokens and reasoning_tokens from usage details."""
        if token_usage is None:
            return None, None

        prompt_tokens_detail_breakdown = getattr(token_usage, "prompt_tokens_details", None)
        cached_tokens = (
            getattr(prompt_tokens_detail_breakdown, "cached_tokens", None)
            if prompt_tokens_detail_breakdown
            else None
        )

        completion_tokens_detail_breakdown = getattr(token_usage, "completion_tokens_details", None)
        reasoning_tokens = (
            getattr(completion_tokens_detail_breakdown, "reasoning_tokens", None)
            if completion_tokens_detail_breakdown
            else None
        )

        return cached_tokens, reasoning_tokens

    def _build_repair_prompt(
        self,
        raw_text: str,
        schema: SchemaSpec,
        validation_errors: list[str],
    ) -> str:
        """Build a prompt asking the model to fix its invalid JSON output."""
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
