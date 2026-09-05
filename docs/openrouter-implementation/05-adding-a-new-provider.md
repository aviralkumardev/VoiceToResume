# Recipe: Adding a New Provider

This walks through adding a hypothetical native `AnthropicProvider` alongside `OpenRouterProvider`, as the concrete proof that the Open/Closed Principle claim in `implementation.md` actually holds: every step below is additive. None of it edits `app/services/llm_providers/provider_interface.py`, `provider_capabilities.py`, `data_models.py`, `provider_exceptions.py`, `json_schema_validation.py`, `provider_factory.py`'s internals, or any existing code that type-hints against `LLMProvider`/`JSONGenerating`/etc.

1. **Create the package.**
   ```
   app/services/llm_providers/anthropic_provider/
   ├── __init__.py
   ├── provider.py
   └── errors.py
   ```

2. **Implement the required contract.** In `provider.py`:
   ```python
   from app.services.llm_providers.provider_interface import LLMProvider

   class AnthropicProvider(LLMProvider):
       provider_name = "anthropic"

       def __init__(self, *, settings=None, async_client=None, model=None, **kwargs) -> None:
           # Same injection shape as OpenRouterProvider: settings/client default
           # from global settings/SDK construction only if not supplied.
           ...

       async def generate_response(self, messages, options=None) -> LLMResponse: ...
   ```
   This method (and `provider_name`) is the *entire* required surface — everything else is opt-in.

3. **Implement only the capabilities you actually support.** Look at `app/services/llm_providers/provider_capabilities.py`'s `JSONGenerating` and `PromptCaching`. These are `Protocol`s, not base classes — `AnthropicProvider` satisfies one purely by having a method or attribute with a matching name/signature, no inheritance or registration step needed. If the Anthropic provider doesn't support prompt caching natively, simply don't set `supports_prompt_caching` — callers do `isinstance(provider, PromptCaching)` to feature-detect, and `AnthropicProvider` correctly reports `False`.

4. **Keep schema handling provider-agnostic where possible, provider-specific where it must be.** `SchemaSpec`/`validate_against_schema` in `json_schema_validation.py` stay untouched — they're already provider-agnostic (a `SchemaSpec` is just a name + JSON Schema dict). What's provider-specific is *how* a `SchemaSpec` gets turned into a request field: OpenRouter uses `response_format: {type: "json_schema", ...}`; Anthropic's native API would use its own tool-use/structured-output shape. That translation is a private method inside `AnthropicProvider` (mirroring `OpenRouterProvider._build_request_kwargs`'s handling of `response_format` in `02-openrouter-provider.md`), not a change to the shared module.

5. **Translate the new SDK's errors into the shared hierarchy.** In `errors.py`:
   ```python
   def translate_anthropic_sdk_error(exc: Exception) -> LLMProviderError:
       # Maps anthropic.APIStatusError subclasses to the exact same
       # provider_exceptions.py classes OpenRouterProvider's errors.py uses —
       # LLMRateLimitError, LLMInvalidRequestError, LLMAuthenticationError,
       # LLMProviderUnavailableError, etc. Do not invent Anthropic-specific
       # exception classes; the whole point of the shared hierarchy is that
       # calling code never needs to know which provider it's talking to.
       ...
   ```
   Use `app/services/llm_providers/openrouter_provider/errors.py` as the reference for the pattern (inspect the SDK's typed exceptions, read retry-after headers, apply any provider-specific heuristics like the temperature-rejection check).

6. **Register it.** In `anthropic_provider/__init__.py`:
   ```python
   from app.services.llm_providers.provider_factory import LLMProviderFactory
   from .provider import AnthropicProvider

   LLMProviderFactory.register("anthropic", AnthropicProvider)

   __all__ = ["AnthropicProvider"]
   ```
   Anything that wants `"anthropic"` available must `import app.services.llm_providers.anthropic_provider` first, same as the OpenRouter package.

7. **Add settings and dependencies.**
   - `requirements.txt`: add the `anthropic` SDK.
   - `app/core/config.py`: add `anthropic_api_key: str` and any Anthropic-specific defaults (e.g. `anthropic_default_model`), following the same naming style as the existing OpenRouter fields.

8. **Verify nothing else needed to change.** Diff your working tree against the base commit — the only new/changed paths should be `app/services/llm_providers/anthropic_provider/*`, `requirements.txt`, and the new `Settings` fields. If you find yourself editing `provider_interface.py`, `provider_capabilities.py`, `provider_factory.py`'s internals, or `OpenRouterProvider`'s code to accommodate the new provider, that's a sign the abstraction in `01-core-abstractions.md` needs to be revisited — it shouldn't be necessary for a provider that fits the same `LLMProvider`/capability shape.
