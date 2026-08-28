"""Small talk that is safer and faster to answer without model inference."""

from __future__ import annotations

import re

_ASSISTANT_SUFFIXES = frozenset({
    "joat",
    "joats",
    "jote",
    "jolt",
    "jokes",
    "jot",
    "there",
})

_RESPONSES = {
    ("good", "morning"): "Good morning. How can I help?",
    ("goodmorning",): "Good morning. How can I help?",
    ("morning",): "Good morning. How can I help?",
    ("good", "afternoon"): "Good afternoon. How can I help?",
    ("goodafternoon",): "Good afternoon. How can I help?",
    ("good", "evening"): "Good evening. How can I help?",
    ("goodevening",): "Good evening. How can I help?",
    ("hello",): "Hello. How can I help?",
    ("hi",): "Hello. How can I help?",
    ("hey",): "Hello. How can I help?",
}


def greeting_response(text: str) -> str | None:
    """Return a deterministic response only when *text* is solely a greeting.

    The wake-word gate normally removes JOAT before command routing, but the
    suffix tolerance also keeps ``--always-answer`` and direct unit use
    natural. Any additional words leave the utterance untouched so a real
    request still reaches the reply engine.
    """

    words = re.findall(r"[a-z0-9]+", text.lower())
    if words and words[-1] in _ASSISTANT_SUFFIXES:
        words.pop()
    return _RESPONSES.get(tuple(words))
