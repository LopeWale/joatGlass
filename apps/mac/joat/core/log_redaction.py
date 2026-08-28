"""Handler-level redaction for secrets carried by WebSocket debug logs."""

from __future__ import annotations

import json
import logging
import re


class SecretRedactionFilter(logging.Filter):
    """Strip pairing and device secrets from request and Frame rendering."""

    _QUERY_PATTERN = re.compile(r"(token=)[^&\s]+")
    _JSON_STRING_PATTERN = re.compile(
        r'("(?:token|deviceToken|challengeId)"\s*:\s*")'
        r'(?:\\.|[^"\\])*'
    )
    _JSON_ARRAY_PATTERN = re.compile(
        r'("deviceTokens"\s*:\s*)\[[^\]]*\]'
    )
    _JSON_SECRET_KEYS = frozenset({"token", "deviceToken", "challengeId"})
    _NAVIGATION_CONTENT_KEYS = frozenset({
        "navigation",
        "plan",
        "latitude",
        "longitude",
        "accuracy",
        "destinationLabel",
        "etaMinutes",
    })

    @classmethod
    def _redact(cls, value: str) -> str:
        value = cls._QUERY_PATTERN.sub(r"\1<redacted>", value)
        value = cls._JSON_STRING_PATTERN.sub(r"\1<redacted>", value)
        return cls._JSON_ARRAY_PATTERN.sub(r'\1["<redacted>"]', value)

    @classmethod
    def _redact_frame_data(cls, value: str) -> str:
        try:
            decoded: object = json.loads(value)
        except json.JSONDecodeError:
            return cls._redact(value)
        navigation_changed = False
        if (
            isinstance(decoded, dict)
            and isinstance(decoded.get("kind"), str)
            and decoded["kind"].startswith("navigation.")
        ):
            decoded = dict(decoded)
            for key in cls._NAVIGATION_CONTENT_KEYS:
                if key in decoded:
                    decoded[key] = "<redacted>"
                    navigation_changed = True
        redacted, changed = cls._redact_json_value(decoded)
        if not changed and not navigation_changed:
            return value
        return json.dumps(redacted, ensure_ascii=False)

    @classmethod
    def _redact_json_value(cls, value: object) -> tuple[object, bool]:
        if isinstance(value, dict):
            changed = False
            redacted: dict[object, object] = {}
            for key, item in value.items():
                if key in cls._JSON_SECRET_KEYS:
                    redacted[key] = "<redacted>"
                    changed = True
                elif key == "deviceTokens":
                    redacted[key] = [] if item == [] else ["<redacted>"]
                    changed = True
                else:
                    redacted_item, item_changed = cls._redact_json_value(item)
                    redacted[key] = redacted_item
                    changed = changed or item_changed
            return redacted, changed
        if isinstance(value, list):
            changed = False
            redacted_items: list[object] = []
            for item in value:
                redacted_item, item_changed = cls._redact_json_value(item)
                redacted_items.append(redacted_item)
                changed = changed or item_changed
            return redacted_items, changed
        return value, False

    @classmethod
    def _redact_arg(cls, value: object, *, render_objects: bool) -> object:
        if isinstance(value, str):
            return cls._redact(value)
        if render_objects:
            frame_data = getattr(value, "data", None)
            if isinstance(frame_data, bytes):
                decoded = frame_data.decode("utf-8", errors="replace")
                redacted_data = cls._redact_frame_data(decoded)
                if redacted_data != decoded:
                    return redacted_data
            elif isinstance(frame_data, str):
                redacted_data = cls._redact_frame_data(frame_data)
                if redacted_data != frame_data:
                    return redacted_data
            rendered = str(value)
            redacted = cls._redact(rendered)
            if redacted != rendered:
                return redacted
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            render_objects = record.name in ("websockets.server", "websockets.client")
            record.args = tuple(
                self._redact_arg(argument, render_objects=render_objects)
                for argument in (
                    record.args if isinstance(record.args, tuple) else (record.args,)
                )
            )
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        return True


def install_websocket_secret_redaction() -> None:
    """Install the filter on handlers and both WebSocket emitting loggers."""

    targets: list[logging.Handler | logging.Logger] = list(
        logging.getLogger().handlers
    )
    targets.extend(
        logging.getLogger(name)
        for name in ("websockets.server", "websockets.client")
    )
    for target in targets:
        if not any(
            isinstance(log_filter, SecretRedactionFilter)
            for log_filter in target.filters
        ):
            target.addFilter(SecretRedactionFilter())
