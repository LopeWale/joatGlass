#!/usr/bin/env python3
"""Check the Mini's private, glasses-first Tailscale Serve boundary.

This is a one-time, read-only deployment check run *on the home Mini*.  It
does not open JOAT's native bridge, connect a WebSocket, read pairing tokens,
or print any existing Tailscale routes.  Its only job is to establish that the
persisted Serve map has the two deliberately separate mounts required by this
project:

* the pre-existing root handler for OpenClaw/Hermes; and
* ``/glasses`` proxied over private HTTPS to JOAT's loopback-only gateway.

The app-level G2 pairing check remains mandatory behind that proxy.  This
script verifies configuration, not a wearer session or a Hermes turn.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Mapping
from typing import Any, Final, NamedTuple


_GLASSES_PATH: Final = "/glasses"
_ROOT_PATH: Final = "/"
_JOAT_LOOPBACK_PROXY: Final = "http://127.0.0.1:8787"
_HTTPS_PORT: Final = "443"
_Run = Callable[..., subprocess.CompletedProcess[str]]


class _Route(NamedTuple):
    """The only Serve fields the preflight needs; never printed verbatim."""

    authority: str
    proxy: str
    https_443: bool
    funnel_enabled: bool


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only private-Tailnet JOAT home-brain preflight",
    )
    parser.add_argument(
        "--tailscale-bin",
        default="tailscale",
        help="Tailscale CLI on this Mini (default: %(default)s)",
    )
    parser.add_argument(
        "--expected-host",
        required=True,
        help="the approved Tailscale hostname for this Mini",
    )
    return parser.parse_args()


def _not_ready(reason: str) -> dict[str, str]:
    """Fixed public result vocabulary; raw Serve JSON must never be echoed."""

    return {"status": "not_ready", "reason": reason}


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _authority(host: str) -> str | None:
    """Normalize one DNS hostname into the only allowed Serve authority."""

    normalized = host.lower().rstrip(".")
    if not normalized or len(normalized) > 253:
        return None
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isascii() and (character.isalnum() or character == "-")
                   for character in label)
        for label in labels
    ):
        return None
    return f"{normalized}:{_HTTPS_PORT}"


def _serve_sections(status: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """Support both node and current Tailscale Service status envelopes."""

    sections: list[Mapping[str, object]] = [status]
    services = _mapping(status.get("Services"))
    if services is not None:
        for raw_service in services.values():
            service = _mapping(raw_service)
            if service is not None:
                sections.append(service)
    return tuple(sections)


def _routes(
    status: Mapping[str, object],
    path: str,
    *,
    expected_authority: str,
) -> tuple[_Route, ...]:
    """Extract the tiny route truth table from ``serve status --json``."""

    routes: list[_Route] = []
    root_funnel = _mapping(status.get("AllowFunnel"))
    for section in _serve_sections(status):
        tcp = _mapping(section.get("TCP"))
        port_443 = _mapping(tcp.get(_HTTPS_PORT)) if tcp is not None else None
        https_443 = port_443 is not None and port_443.get("HTTPS") is True
        web = _mapping(section.get("Web"))
        funnel = _mapping(section.get("AllowFunnel"))
        if web is None:
            continue
        for authority, web_config_raw in web.items():
            if not isinstance(authority, str) or authority.lower() != expected_authority:
                continue
            web_config = _mapping(web_config_raw)
            handlers = _mapping(web_config.get("Handlers")) if web_config is not None else None
            handler = _mapping(handlers.get(path)) if handlers is not None else None
            proxy = handler.get("Proxy") if handler is not None else None
            if not isinstance(proxy, str):
                continue
            routes.append(_Route(
                authority=authority,
                proxy=proxy,
                https_443=https_443,
                funnel_enabled=(
                    (funnel is not None and funnel.get(authority) is True)
                    or (root_funnel is not None and root_funnel.get(authority) is True)
                ),
            ))
    return tuple(routes)


def inspect_serve_status(*, status: object, expected_host: str) -> dict[str, str]:
    """Return a bounded deployment classification from untrusted CLI JSON."""

    root = _mapping(status)
    if root is None:
        return _not_ready("serve_status_invalid")
    expected_authority = _authority(expected_host)
    if expected_authority is None:
        return _not_ready("expected_host_invalid")
    glasses = _routes(root, _GLASSES_PATH, expected_authority=expected_authority)
    if not glasses:
        return _not_ready("glasses_route_missing")
    if not any(route.proxy == _JOAT_LOOPBACK_PROXY for route in glasses):
        return _not_ready("glasses_route_wrong_backend")
    configured = tuple(route for route in glasses if route.proxy == _JOAT_LOOPBACK_PROXY)
    if not any(route.https_443 for route in configured):
        return _not_ready("https_443_missing")
    if any(route.funnel_enabled for route in configured):
        return _not_ready("funnel_enabled")
    if not _routes(root, _ROOT_PATH, expected_authority=expected_authority):
        return _not_ready("root_route_missing")
    return {
        "status": "ready",
        "glasses_route": "verified",
        "root_route": "verified",
        "tls": "verified",
        "tailnet_only": "verified",
    }


def probe(
    *,
    tailscale_bin: str = "tailscale",
    expected_host: str,
    run: _Run = subprocess.run,
) -> dict[str, str]:
    """Read local Serve state only; do not mutate Tailscale or JOAT."""

    try:
        completed = run(
            [tailscale_bin, "serve", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return _not_ready("tailscale_unavailable")
    except (OSError, subprocess.TimeoutExpired):
        return _not_ready("tailscale_status_unavailable")
    if completed.returncode != 0:
        return _not_ready("tailscale_status_unavailable")
    try:
        status: Any = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return _not_ready("serve_status_invalid")
    return inspect_serve_status(status=status, expected_host=expected_host)


def main() -> int:
    args = _arguments()
    result = probe(tailscale_bin=args.tailscale_bin, expected_host=args.expected_host)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
