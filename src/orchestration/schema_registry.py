"""Minimal schema registry (Plan §7, Phase 1).

Provides ``register`` / ``validate`` with a deliberately small validation model
(required fields + basic type checks), plus optional schema-specific semantic
validators for cross-field invariants. The interface is shaped so it can later
be swapped for ``jsonschema`` without callers changing.

Schema format (minimal subset)::

    {
        "required": ["name", "age"],
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "additional_properties": True,  # optional, default True
        "semantic_validator": callable,  # optional, returns a list of errors
    }
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

SemanticValidator = Callable[[Dict[str, Any]], List[str]]

_DEFAULT_SEMANTIC_VALIDATORS: Dict[str, SemanticValidator] = {}


def register_default_semantic_validators(
    validators: Dict[str, SemanticValidator],
) -> None:
    """Register semantic validators used by versioned built-in schemas."""
    for schema_ref, validator in validators.items():
        if not isinstance(schema_ref, str) or not schema_ref:
            raise ValueError("schema_ref must be a non-empty string")
        if not callable(validator):
            raise TypeError("semantic validators must be callable")
        _DEFAULT_SEMANTIC_VALIDATORS[schema_ref] = validator


def _resolve_default_semantic_validator(
    schema_ref: str,
) -> SemanticValidator | None:
    """Resolve a built-in validator without relying on import order.

    The contract catalog normally registers validators during import. A caller
    may, however, instantiate a fresh ``SchemaRegistry`` and register a known
    contract directly before importing the catalog. Lazy resolution keeps that
    direct API fail-closed while avoiding a module-level circular import.
    """

    validator = _DEFAULT_SEMANTIC_VALIDATORS.get(schema_ref)
    if validator is not None:
        return validator
    try:
        from src.contracts.agent_schema_catalog import AGENT_SCHEMA_VALIDATORS
    except (AttributeError, ImportError):
        return None

    validator = AGENT_SCHEMA_VALIDATORS.get(schema_ref)
    if validator is not None:
        _DEFAULT_SEMANTIC_VALIDATORS[schema_ref] = validator
    return validator


def _compose_semantic_validators(
    *validators: SemanticValidator,
) -> SemanticValidator:
    """Run multiple validators while preserving built-in invariants."""

    def validate(payload: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        for validator in validators:
            errors.extend(validator(payload))
        return errors

    return validate


from src.orchestration.output_contracts import OUTPUT_SCHEMAS

# Accepted "type" tokens -> python types. ``number`` accepts int or float.
_TYPE_MAP: Dict[str, tuple] = {
    "string": (str,),
    "str": (str,),
    "integer": (int,),
    "int": (int,),
    "number": (int, float),
    "float": (float, int),
    "boolean": (bool,),
    "bool": (bool,),
    "array": (list,),
    "list": (list,),
    "object": (dict,),
    "dict": (dict,),
    "null": (type(None),),
}


def _validate_value(
    value: Any,
    spec: Dict[str, Any],
    path: str,
    errors: List[str],
) -> None:
    expected = spec.get("type")
    if expected:
        allowed = _TYPE_MAP.get(str(expected).lower())
        if allowed is None:
            errors.append(f"{path}: unknown type {expected!r} in schema")
            return
        if isinstance(value, bool) and bool not in allowed:
            errors.append(f"{path}: expected {expected}, got bool")
            return
        if not isinstance(value, allowed):
            errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
            return

    enum = spec.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: expected one of {enum!r}, got {value!r}")

    if isinstance(value, dict):
        required = spec.get("required", []) or []
        for field in required:
            if field not in value:
                errors.append(f"{path}: missing required field: {field!r}")
        properties: Dict[str, Any] = spec.get("properties", {}) or {}
        for field, field_spec in properties.items():
            if field in value:
                _validate_value(
                    value[field],
                    field_spec or {},
                    f"{path}.{field}",
                    errors,
                )
        if not spec.get("additional_properties", True):
            for field in value:
                if field not in properties:
                    errors.append(f"{path}: unexpected field: {field!r}")

    if isinstance(value, list) and spec.get("items"):
        item_spec = spec["items"] or {}
        for index, item in enumerate(value):
            _validate_value(item, item_spec, f"{path}[{index}]", errors)

    semantic_validator = spec.get("semantic_validator")
    if semantic_validator is not None:
        if not callable(semantic_validator):
            errors.append(f"{path}: semantic_validator must be callable")
        else:
            errors.extend(semantic_validator(value, path))


class SchemaRegistry:
    """In-memory registry of named schemas with minimal validation."""

    def __init__(self) -> None:
        self._schemas: Dict[str, Dict[str, Any]] = {}
        self._semantic_validators: Dict[str, SemanticValidator] = {}

    def register(
        self,
        schema_ref: str,
        schema: Dict[str, Any],
        *,
        semantic_validator: SemanticValidator | None = None,
    ) -> None:
        """Register a schema and its optional cross-field validator."""
        if not isinstance(schema_ref, str) or not schema_ref:
            raise ValueError("schema_ref must be a non-empty string")
        if not isinstance(schema, dict):
            raise TypeError("schema must be a dict")
        if semantic_validator is not None and not callable(semantic_validator):
            raise TypeError("semantic_validator must be callable")
        self._schemas[schema_ref] = schema
        builtin_validator = _resolve_default_semantic_validator(schema_ref)
        if builtin_validator is not None:
            if semantic_validator is None or semantic_validator is builtin_validator:
                semantic_validator = builtin_validator
            else:
                # A caller may add stricter checks, but cannot replace the
                # platform invariant for a versioned built-in contract.
                semantic_validator = _compose_semantic_validators(
                    builtin_validator,
                    semantic_validator,
                )
        if semantic_validator is None:
            self._semantic_validators.pop(schema_ref, None)
        else:
            self._semantic_validators[schema_ref] = semantic_validator

    def set_semantic_validator(
        self,
        schema_ref: str,
        semantic_validator: SemanticValidator,
    ) -> None:
        """Attach a semantic validator without replacing the registered schema."""
        if not isinstance(schema_ref, str) or not schema_ref:
            raise ValueError("schema_ref must be a non-empty string")
        if schema_ref not in self._schemas:
            raise KeyError(f"unknown schema_ref: {schema_ref!r}")
        if not callable(semantic_validator):
            raise TypeError("semantic_validator must be callable")
        builtin_validator = _resolve_default_semantic_validator(schema_ref)
        if (
            builtin_validator is not None
            and semantic_validator is not builtin_validator
        ):
            semantic_validator = _compose_semantic_validators(
                builtin_validator,
                semantic_validator,
            )
        self._semantic_validators[schema_ref] = semantic_validator

    def has(self, schema_ref: str) -> bool:
        return schema_ref in self._schemas

    def get(self, schema_ref: str) -> Dict[str, Any] | None:
        return self._schemas.get(schema_ref)

    def validate(self, payload: Any, schema_ref: str) -> Tuple[bool, List[str]]:
        """Validate ``payload`` against a registered schema.

        Returns ``(is_valid, errors)``. An unknown ``schema_ref`` is reported as
        invalid so callers never silently pass unchecked data.
        """
        schema = self._schemas.get(schema_ref)
        if schema is None:
            return False, [f"unknown schema_ref: {schema_ref!r}"]

        errors: List[str] = []

        top_level_type = str(schema.get("type") or "object").lower()
        allowed_top_level = _TYPE_MAP.get(top_level_type)
        if allowed_top_level is None:
            return False, [
                f"unknown top-level type {top_level_type!r} in schema {schema_ref!r}"
            ]
        if not isinstance(payload, allowed_top_level):
            return False, [
                f"payload must be {top_level_type} for schema {schema_ref!r}, "
                f"got {type(payload).__name__}"
            ]

        _validate_value(payload, schema, "payload", errors)

        semantic_validator = self._semantic_validators.get(schema_ref)
        if semantic_validator is not None and not errors:
            errors.extend(semantic_validator(payload))

        return (len(errors) == 0), errors


# Process-wide default registry (optional convenience for non-test callers).
_DEFAULT_REGISTRY = SchemaRegistry()
for _schema_ref, _schema in OUTPUT_SCHEMAS.items():
    _DEFAULT_REGISTRY.register(_schema_ref, _schema)


def get_schema_registry() -> SchemaRegistry:
    """Return the process-wide default :class:`SchemaRegistry`."""
    return _DEFAULT_REGISTRY
