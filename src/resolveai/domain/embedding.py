"""Deterministic embeddings for offline retrieval.

Live embedding providers stay blocked while the budget is zero. These vectors
only order the local policy corpus; they are not a claim about a hosted model.
"""

from __future__ import annotations

import math
import re
import zlib

DIMENSIONS = 32
_TOKEN = re.compile(r"[a-z0-9]+")


def embed_text(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for token in _TOKEN.findall(text.lower()):
        slot = zlib.crc32(token.encode("utf-8")) % DIMENSIONS
        vector[slot] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"
