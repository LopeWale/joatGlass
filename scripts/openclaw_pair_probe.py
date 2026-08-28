#!/usr/bin/env python3
"""Create or verify JOAT's read-only OpenClaw device pairing.

This command never accepts a Telegram token or shared Gateway secret.  It is
for the managed Tailscale Serve route, where tailnet identity authenticates the
transport and the Gateway separately approves JOAT's stable device identity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from joat.agents.openclaw_gateway import (
    GatewayProfile,
    OpenClawGatewayClient,
    OpenClawPairingRequired,
)
from joat.agents.openclaw_native import NativeOpenClawCredentials
from joat.native.bridge import NativeBridgeClient


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Request or verify a read-only JOAT OpenClaw device pairing"
    )
    parser.add_argument("--url", required=True, help="private wss:// Tailscale Serve origin")
    parser.add_argument(
        "--bridge",
        type=Path,
        default=Path(
            os.environ.get(
                "JOAT_NATIVE_BRIDGE",
                str(
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "JOAT"
                    / "native-bridge.sock"
                ),
            )
        ),
        help="owner-only JOAT native bridge socket",
    )
    return parser.parse_args()


async def _probe(*, url: str, bridge_path: Path) -> dict[str, object]:
    profile = GatewayProfile(url)
    async with NativeBridgeClient(bridge_path) as bridge:
        credentials = await NativeOpenClawCredentials.open(
            bridge=bridge,
            profile=profile,
        )
        client = OpenClawGatewayClient(
            profile=profile,
            identity=credentials,
            token_store=credentials,
            scopes=("operator.read",),
        )
        try:
            connection = await client.connect()
        except OpenClawPairingRequired as exc:
            return {
                "status": "approval_required",
                "gateway": profile.url,
                "device_id": credentials.device_id,
                "request_id": exc.request_id,
                "reason": exc.reason,
                "requested_scopes": ["operator.read"],
            }
        try:
            server = connection.hello.get("server")
            auth = connection.hello.get("auth")
            return {
                "status": "connected_read_only",
                "gateway": profile.url,
                "device_id": credentials.device_id,
                "server": server,
                "auth": auth,
                "advertised_methods": sorted(connection.advertised_methods),
            }
        finally:
            await connection.close()


def main() -> int:
    args = _arguments()
    result = asyncio.run(_probe(url=args.url, bridge_path=args.bridge))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
