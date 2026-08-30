"""Contracts for the read-only private-Tailnet home-brain preflight."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any


def _preflight_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "tailnet_home_brain_preflight.py"
    spec = importlib.util.spec_from_file_location("tailnet_home_brain_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve_status(*, envelope: bool = False) -> dict[str, object]:
    section: dict[str, object] = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "admins-mac-mini.tail3a3363.ts.net:443": {
                "Handlers": {
                    "/": {"Proxy": "http://127.0.0.1:9443"},
                    "/glasses": {"Proxy": "http://127.0.0.1:8787"},
                },
            },
        },
        "AllowFunnel": {"admins-mac-mini.tail3a3363.ts.net:443": False},
    }
    return {"Services": {"svc:home-brain": section}} if envelope else section


def test_inspection_accepts_node_and_service_status_envelopes_without_echoing_routes() -> None:
    preflight = _preflight_module()
    expected_host = "admins-mac-mini.tail3a3363.ts.net"
    expected = {
        "status": "ready",
        "glasses_route": "verified",
        "root_route": "verified",
        "tls": "verified",
        "tailnet_only": "verified",
    }

    assert preflight.inspect_serve_status(status=_serve_status(), expected_host=expected_host) == expected
    assert preflight.inspect_serve_status(
        status=_serve_status(envelope=True), expected_host=expected_host,
    ) == expected
    assert "admins-mac-mini" not in json.dumps(expected)
    assert "8787" not in json.dumps(expected)


def test_inspection_rejects_the_exact_missing_glasses_route_seen_on_the_mini() -> None:
    preflight = _preflight_module()
    status = _serve_status()
    web = status["Web"]
    assert isinstance(web, dict)
    authority = next(iter(web.values()))
    assert isinstance(authority, dict)
    handlers = authority["Handlers"]
    assert isinstance(handlers, dict)
    handlers.pop("/glasses")

    assert preflight.inspect_serve_status(
        status=status, expected_host="admins-mac-mini.tail3a3363.ts.net",
    ) == {
        "status": "not_ready", "reason": "glasses_route_missing",
    }


def test_inspection_fails_closed_on_wrong_backend_tls_funnel_or_lost_root() -> None:
    preflight = _preflight_module()
    status = _serve_status()
    web = status["Web"]
    assert isinstance(web, dict)
    authority = next(iter(web.values()))
    assert isinstance(authority, dict)
    handlers = authority["Handlers"]
    assert isinstance(handlers, dict)
    glasses = handlers["/glasses"]
    assert isinstance(glasses, dict)
    glasses["Proxy"] = "http://127.0.0.1:9443"
    assert preflight.inspect_serve_status(
        status=status, expected_host="admins-mac-mini.tail3a3363.ts.net",
    ) == {
        "status": "not_ready", "reason": "glasses_route_wrong_backend",
    }

    glasses["Proxy"] = "http://127.0.0.1:8787"
    tcp = status["TCP"]
    assert isinstance(tcp, dict)
    tls = tcp["443"]
    assert isinstance(tls, dict)
    tls["HTTPS"] = False
    assert preflight.inspect_serve_status(
        status=status, expected_host="admins-mac-mini.tail3a3363.ts.net",
    ) == {
        "status": "not_ready", "reason": "https_443_missing",
    }

    tls["HTTPS"] = True
    funnel = status["AllowFunnel"]
    assert isinstance(funnel, dict)
    funnel["admins-mac-mini.tail3a3363.ts.net:443"] = True
    assert preflight.inspect_serve_status(
        status=status, expected_host="admins-mac-mini.tail3a3363.ts.net",
    ) == {
        "status": "not_ready", "reason": "funnel_enabled",
    }

    funnel["admins-mac-mini.tail3a3363.ts.net:443"] = False
    handlers.pop("/")
    assert preflight.inspect_serve_status(
        status=status, expected_host="admins-mac-mini.tail3a3363.ts.net",
    ) == {
        "status": "not_ready", "reason": "root_route_missing",
    }


def test_probe_handles_missing_cli_and_never_echoes_cli_output() -> None:
    preflight = _preflight_module()

    def missing(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    assert preflight.probe(expected_host="admins-mac-mini.tail3a3363.ts.net", run=missing) == {
        "status": "not_ready", "reason": "tailscale_unavailable",
    }

    def hostile(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="token=private", stderr="private route",
        )

    result = preflight.probe(expected_host="admins-mac-mini.tail3a3363.ts.net", run=hostile)
    assert result == {"status": "not_ready", "reason": "tailscale_status_unavailable"}
    assert "private" not in json.dumps(result)


def test_probe_reads_only_the_serve_status_command() -> None:
    preflight = _preflight_module()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps(_serve_status()), stderr="",
        )

    assert preflight.probe(
        tailscale_bin="/usr/local/bin/tailscale",
        expected_host="ADMINS-MAC-MINI.TAIL3A3363.TS.NET.",
        run=runner,
    )["status"] == "ready"
    assert calls == [(
        ["/usr/local/bin/tailscale", "serve", "status", "--json"],
        {"check": False, "capture_output": True, "text": True, "timeout": 5},
    )]


def test_preflight_never_accepts_a_route_on_a_different_mini_or_global_funnel() -> None:
    preflight = _preflight_module()
    status = _serve_status()

    assert preflight.inspect_serve_status(
        status=status, expected_host="other-mini.tail3a3363.ts.net",
    ) == {"status": "not_ready", "reason": "glasses_route_missing"}
    assert preflight.inspect_serve_status(
        status=status, expected_host="not a hostname",
    ) == {"status": "not_ready", "reason": "expected_host_invalid"}

    nested = {
        "AllowFunnel": {"admins-mac-mini.tail3a3363.ts.net:443": True},
        "Services": {"svc:home-brain": _serve_status()},
    }
    assert preflight.inspect_serve_status(
        status=nested, expected_host="admins-mac-mini.tail3a3363.ts.net",
    ) == {"status": "not_ready", "reason": "funnel_enabled"}
