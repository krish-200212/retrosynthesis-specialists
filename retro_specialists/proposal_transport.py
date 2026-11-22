"""Bound object transport; route schema and chemistry checks remain in the backend."""
import json
import math

_MAX_PROPOSAL_BYTES = 1_000_000
_MAX_NODES = 100_000
_MAX_DEPTH = 32


def _bounded_json_object(value):
    """Check resource bounds before serialization, without copying or repairing data."""
    stack = [iter((value,))]
    nodes = text_size = 0
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        nodes += 1
        if nodes > _MAX_NODES or len(stack) > _MAX_DEPTH:
            raise ValueError('Proposal object exceeds structural transport limits')
        if isinstance(item, str):
            text_size += len(item)
            if text_size > _MAX_PROPOSAL_BYTES:
                raise ValueError('Proposal object exceeds text transport limit')
        elif isinstance(item, dict):
            if len(item) > _MAX_NODES or any(not isinstance(key, str) for key in item):
                raise ValueError('Proposal object requires bounded string keys')
            stack.append(iter(part for pair in item.items() for part in pair))
        elif isinstance(item, list):
            if len(item) > _MAX_NODES:
                raise ValueError('Proposal array exceeds transport limit')
            stack.append(iter(item))
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError('Proposal object contains a non-JSON value')
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError('Proposal object contains a nonfinite number')
    return _encode_bounded(value)


def _encode_bounded(value):
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False,
                               sort_keys=True, separators=(',', ':'))
    chunks, byte_count = [], 0
    for chunk in encoder.iterencode(value):
        byte_count += len(chunk)  # ensure_ascii makes characters equal bytes
        if byte_count > _MAX_PROPOSAL_BYTES:
            raise ValueError('Proposal object exceeds serialized byte limit')
        chunks.append(chunk)
    return ''.join(chunks)


def normalize_proposal_arguments(action, arguments):
    """Preserve legacy JSON text byte-for-byte; canonicalize only object inputs."""
    field = {'validate_route_hypotheses': 'raw_response',
             'verify_retrosynthesis_result': 'final_response'}.get(action)
    if not isinstance(arguments, dict) or field is None or not isinstance(arguments.get(field), dict):
        return arguments
    return {**arguments, field: _bounded_json_object(arguments[field])}


def proposal_argument_schema():
    """Fresh, exact object contract plus the unchanged legacy text alternative."""
    from .contracts import response_schema
    return {'oneOf': [response_schema(20, 100, require_stereo_plan=True,
                                     require_origin_declarations=True),
                      {'type': 'string', 'maxLength': _MAX_PROPOSAL_BYTES}]}
