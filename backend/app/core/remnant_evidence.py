"""Dedicated typed ASCII evidence hashing; existing canonical hashes are untouched."""

import hashlib
import json
import math
import struct
from typing import Any

PREFIX = 'werco-remnant-evidence-v1\n'
MAX_SAFE_INTEGER = 9007199254740991
MAX_CANONICAL_BYTES = 32 * 1024 * 1024
MAX_NODES = 1000000
MAX_DEPTH = 32


def canonical_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'))


def canonical_evidence(value: Any) -> str:
    """Encode JSON values identically to JS, including UTF-16 and binary64 numbers."""
    pieces = [PREFIX]
    size = len(PREFIX)
    nodes = 0

    def emit(text: str) -> None:
        nonlocal size
        size += len(text)
        if size > MAX_CANONICAL_BYTES:
            raise ValueError('Remnant canonical evidence exceeds its byte budget')
        pieces.append(text)

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ValueError('Remnant canonical evidence exceeds its structural budget')
        if item is None:
            emit('n')
        elif type(item) is bool:
            emit('b1' if item else 'b0')
        elif type(item) in (int, float):
            if type(item) is int and abs(item) > MAX_SAFE_INTEGER:
                raise ValueError('Remnant integer exceeds the safe binary64 range')
            number = float(item)
            if not math.isfinite(number) or (number.is_integer() and abs(number) > MAX_SAFE_INTEGER):
                raise ValueError('Remnant numbers must be finite and integers safely representable')
            emit('d' + struct.pack('>d', number if number else 0.0).hex())
        elif type(item) is str:
            emit('s' + canonical_string(item))
        elif type(item) is list:
            emit('a[')
            for index, child in enumerate(item):
                if index:
                    emit(',')
                visit(child, depth + 1)
            emit(']')
        elif type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError('Remnant object keys must be strings')
            keys = sorted((canonical_string(key), key) for key in item)
            if len({encoded for encoded, _ in keys}) != len(keys):
                raise ValueError('Remnant object keys must be distinct in UTF-16')
            emit('o{')
            for index, (key, original) in enumerate(keys):
                if index:
                    emit(',')
                emit(key + ':')
                visit(item[original], depth + 1)
            emit('}')
        else:
            raise ValueError('Remnant evidence must contain JSON values only')

    visit(value, 0)
    return ''.join(pieces)


def evidence_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_evidence(value).encode('ascii')).hexdigest()


def target_group_sha256(group_id: str, required_grade: str, quote: dict) -> str:
    """Raw imperial quote is intentional: unit conversion must follow this check."""
    return evidence_sha256({'groupId': group_id, 'requiredGrade': required_grade, 'quote': quote})
