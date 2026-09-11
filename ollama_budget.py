"""Conservative request sizing and context growth isolated to one analysis job.

No tokenizer is downloaded and no probe generation is sent. UTF-8 byte counts
provide a deliberately generous input estimate for byte-fallback tokenizers;
this is not a measured token count. Actual Ollama counts are logged separately.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class ContextBudgetExceeded(ValueError):
    """The estimated request cannot fit without losing source or output space."""


@dataclass
class ContextGrowth:
    size: int = 0


_growth: ContextVar[ContextGrowth | None] = ContextVar("ollama_analysis_context_growth", default=None)
CONTEXT_TIERS = (8_192, 16_384, 32_768, 65_536, 131_072)
SAFETY_TOKENS = 2_048


@contextmanager
def ollama_context_scope():
    """Retain a grown context for a job, resetting it even on failure/cancel."""
    token = _growth.set(ContextGrowth())
    try:
        yield
    finally:
        _growth.reset(token)


def choose_analysis_context(messages: list[dict[str, str]], response_format: object,
                            output_tokens: int, *, minimum: int, maximum: int) -> tuple[int, int]:
    """Return (allocated context, estimated input) for the fully assembled prompt."""
    if not 0 < minimum <= maximum or output_tokens < 1:
        raise ValueError("Invalid analysis context limits")
    # Include role/template overhead and the schema even if a backend implements
    # its constraints without embedding the entire schema in the prompt.
    input_estimate = sum(len(message.get("content", "").encode("utf-8")) + 64 for message in messages)
    if response_format is not None:
        input_estimate += len(json.dumps(response_format, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    required = input_estimate + output_tokens + SAFETY_TOKENS
    if required > maximum:
        raise ContextBudgetExceeded(
            f"Analysis request budget {required} exceeds the {maximum}-token context ceiling "
            "using a conservative UTF-8 estimate; source was not silently truncated. "
            "Reduce dependency context or raise OLLAMA_ANALYSIS_CONTEXT_MAX."
        )
    tiers = sorted({minimum, maximum, *(tier for tier in CONTEXT_TIERS if minimum <= tier <= maximum)})
    size = next(tier for tier in tiers if tier >= required)
    growth = _growth.get()
    if growth is not None:
        size = max(size, min(growth.size, maximum))
        growth.size = size
    return size, input_estimate
