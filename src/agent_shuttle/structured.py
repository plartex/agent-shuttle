"""Bounded, local JSON schemas for native structured output."""

import json

from jsonschema import Draft202012Validator, SchemaError, ValidationError


def encode_output_schema(schema):
    if schema is None:
        return None
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("output_schema must be a JSON object schema with type=object")
    try:
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("output_schema must contain JSON values") from exc
    if len(encoded.encode()) > 65536:
        raise ValueError("output_schema exceeds 64 KiB")

    def local_references(value):
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                ref = value.get(key)
                if ref is not None and (not isinstance(ref, str) or not ref.startswith("#/")):
                    raise ValueError("output_schema may only use local references")
            for child in value.values():
                local_references(child)
        elif isinstance(value, list):
            for child in value:
                local_references(child)

    local_references(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid output_schema: {exc.message}") from exc
    return encoded


def validate_output(value, schema):
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise ValueError(f"invalid structured output at {list(exc.absolute_path)}: {exc.message}") from exc
    return value
