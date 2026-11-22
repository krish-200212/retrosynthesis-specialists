"""Strict, independently allocated provider response contracts.

This schema validates representation only. Neither JSON validity nor a model's
``self_assessment`` establishes chemical feasibility.
"""
from __future__ import annotations

import json


MAX_RESPONSE_BYTES = 1_048_576
MAX_SMILES_CHARS = 4096


class ContractError(ValueError):
    """Untrusted response rejection, with any safely parsed proposal count."""

    def __init__(self, message: str, proposed: int = 0):
        super().__init__(message)
        self.proposed = proposed


def positive_limit(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in 1..{maximum}")
    return value


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def response_schema(requested_count: int = 5, max_total_reactions: int = 20,
                    *, require_stereo_plan: bool = False,
                    require_origin_declarations: bool = False) -> dict:
    """Return a fresh schema; never mutate shared schemas across requests."""
    positive_limit(requested_count, "requested_count", 20)
    positive_limit(max_total_reactions, "max_total_reactions", 200)
    if type(require_stereo_plan) is not bool:
        raise ValueError('Stereo-plan requirement must be a boolean')
    if type(require_origin_declarations) is not bool:
        raise ValueError('Origin-declaration requirement must be a boolean')
    from .stereochemistry import stereo_plan_schema
    smiles = {"type": "string", "minLength": 1, "maxLength": MAX_SMILES_CHARS}
    text = {"type": "string", "minLength": 1, "maxLength": 8000}
    reaction = _object({"product": dict(smiles), "reactants": {
        "type": "array", "minItems": 1, "maxItems": 32, "items": dict(smiles)}})
    route = _object({
        "route_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "name": {"type": "string", "minLength": 1, "maxLength": 512},
        "reactions": {"type": "array", "minItems": 1,
                      "maxItems": max_total_reactions, "items": reaction},
        "rationale": dict(text),
        "feasibility_risks": {"type": "array", "maxItems": 32, "items": dict(text)},
        "evidence_ids": {"type": "array", "maxItems": 64, "uniqueItems": True,
                         "items": {"type": "string", "minLength": 1, "maxLength": 256}},
        "self_assessment": {"type": "string", "enum": ["proposed", "withdrawn"]},
    })
    route['properties']['stereo_plan'] = stereo_plan_schema(
        require_origin_declarations=require_origin_declarations)
    if require_stereo_plan or require_origin_declarations:
        route['required'].append('stereo_plan')
    return _object({
        "routes": {"type": "array", "maxItems": requested_count, "items": route},
        "limitations": {"type": "array", "maxItems": 32, "items": dict(text)},
    })


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ContractError("Duplicate JSON object key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ContractError("Nonfinite JSON number")


def parse_response(raw: str, requested_count: int = 5,
                   max_total_reactions: int = 20, *, require_stereo_plan: bool = False,
                   require_origin_declarations: bool = False) -> dict:
    from jsonschema import Draft202012Validator
    schema = response_schema(requested_count, max_total_reactions,
                             require_stereo_plan=require_stereo_plan,
                             require_origin_declarations=require_origin_declarations)
    if not isinstance(raw, str):
        raise ContractError("Provider response must be JSON text")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeError as exc:
        raise ContractError("Invalid response text encoding") from exc
    if size > MAX_RESPONSE_BYTES:
        raise ContractError("Provider response exceeds byte limit")
    try:
        parsed = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ContractError("Invalid JSON response") from exc
    proposed = len(parsed.get("routes", [])) if isinstance(parsed, dict) and isinstance(parsed.get("routes"), list) else 0
    error = next(Draft202012Validator(schema).iter_errors(parsed), None)
    if error is not None:
        path = "/".join(str(part) for part in error.absolute_path)
        raise ContractError(f"Schema rejection at /{path}: {error.validator}", proposed)
    return parsed
