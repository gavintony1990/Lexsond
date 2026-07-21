from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArithmeticChallenge:
    prompt: str
    expected_text: str


_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"
)
_TEENS = (
    "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
def arithmetic_challenge(seed: str) -> ArithmeticChallenge:
    """Create a deterministic moving challenge without embedding its answer.

    Scheduled-slot UUIDs are stable across dispatch retries, so deriving from
    that seed keeps one billable attempt idempotent while rotating later slots.
    """

    if not isinstance(seed, str) or not seed or len(seed) > 512:
        raise ValueError("challenge seed must be a bounded non-empty string")
    digest = hashlib.sha256(f"lexsond-monitor-challenge:{seed}".encode()).digest()
    nonce = digest[:16].hex()
    left = 10 + digest[16] % 90
    right = 10 + digest[17] % 90
    variants = (
        f"Calculate {left} plus {right}.",
        f"One tray contains {left} items and another contains {right} items. Give their total.",
        f"Find the sum of {_spell(left)} and {_spell(right)}.",
        f"Add {_spell(right)} to {_spell(left)}.",
    )
    expected = f"LEXSOND_RESULT={left + right};NONCE={nonce}"
    prompt = (
        variants[digest[18] % len(variants)]
        + " Return exactly LEXSOND_RESULT=<total>;NONCE="
        + nonce
        + ", replacing <total> with the sum in digits and adding no other text."
    )
    if expected in prompt:
        raise RuntimeError("challenge invariant violated: answer leaked into prompt")
    return ArithmeticChallenge(prompt=prompt, expected_text=expected)


def _spell(value: int) -> str:
    if not 10 <= value <= 99:
        raise ValueError("challenge operands must be between 10 and 99")
    if value < 20:
        return _TEENS[value - 10]
    tens = _TENS[value // 10]
    return tens if value % 10 == 0 else f"{tens}-{_ONES[value % 10]}"
