from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Type

from jsonschema import Draft7Validator
from pydantic import BaseModel


@dataclass(frozen=True)
class SchemaSpec:

    name: str
    json_schema: dict[str, Any]
    strict: bool = True

    @classmethod
    def from_pydantic(
        cls, model: Type[BaseModel], *, name: Optional[str] = None, strict: bool = True
    ) -> "SchemaSpec":
        """Construct a SchemaSpec from a pydantic BaseModel subclass."""
        generated_json_schema = model.model_json_schema()
        generated_json_schema.setdefault("additionalProperties", False)
        return cls(
            name=name or model.__name__,
            json_schema=generated_json_schema,
            strict=strict,
        )

    @classmethod
    def from_dict(
        cls, schema: dict[str, Any], *, name: str, strict: bool = True
    ) -> "SchemaSpec":
        """Construct a SchemaSpec from a raw JSON Schema dict."""
        return cls(name=name, json_schema=schema, strict=strict)


def build_response_format(
    schema_spec: SchemaSpec, *, response_format_mode: str = "json_schema"
) -> dict[str, Any]:
    """Build the `response_format` request field.

    response_format_mode="json_schema" -> strict structured output, enforced
    provider-side (when the model supports it) and always re-validated locally
    as defense in depth.
    response_format_mode="json_object" -> basic JSON mode; the provider only
    guarantees syntactically valid JSON, so validate_against_schema() below is
    the only schema enforcement that actually happens.
    """
    if response_format_mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_spec.name,
                "strict": schema_spec.strict,
                "schema": schema_spec.json_schema,
            },
        }
    if response_format_mode == "json_object":
        return {"type": "json_object"}
    raise ValueError(f"Unknown response_format mode: {response_format_mode!r}")


def validate_against_schema(json_payload: dict[str, Any], schema_spec: SchemaSpec) -> list[str]:
    json_schema_validator = Draft7Validator(schema_spec.json_schema)
    validation_errors = sorted(
        json_schema_validator.iter_errors(json_payload),
        key=lambda error: list(error.path),
    )
    return [
        f"{'/'.join(str(path_part) for path_part in error.path) or '<root>'}: {error.message}"
        for error in validation_errors
    ]
