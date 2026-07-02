"""
Shared low-level plumbing for the backend integrations.

One copy of everything app.py (the status sweep), ui.py (drill-down screens),
and probe.py (admin Test buttons) all need: auth-header builders, the
unwrap-"data" GET, the TrueNAS JSON-RPC-over-WebSocket session, UniFi site and
gateway resolution, and the small formatting helpers.

Also owns the shared persistent httpx client. Backends live on the LAN with
self-signed certs, so verification is off; pooling means the UniFi fast lane
(polled every second by the panels) reuses one TLS connection instead of
handshaking per poll. app.py closes it via the FastAPI lifespan hook.
"""

import asyncio
import json
import ssl
from contextlib import asynccontextmanager

import httpx
import websockets

# --- formatting helpers ------------------------------------------------------

def pct(used: float, total: float) -> int:
    if not total:
        return 0
    return max(0, min(100, round(used / total * 100)))


def format_bytes(n) -> str:
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return f"{n:.0f}{u}" if u in ("B", "K", "M") else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


def duration(sec: int) -> str:
    if sec <= 0:
        return "-"
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# --- TLS / clients ------------------------------------------------------------

def insecure_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_client: "httpx.AsyncClient | None" = None


def client() -> httpx.AsyncClient:
    """The shared pooled client. Lazy so tests/imports don't need a loop."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(verify=False)
    return _client


async def close_client() -> None:
    if _client is not None and not _client.is_closed:
        await _client.aclose()


# --- bases and auth headers ---------------------------------------------------

def pve_base(host: str) -> str:
    return f"https://{host}/api2/json"


def pbs_base(host: str) -> str:
    return f"https://{host}/api2/json"


def unifi_base(host: str) -> str:
    return f"https://{host}/proxy/network/integration/v1"


def pve_headers(token_id: str, secret: str) -> dict:
    return {"Authorization": f"PVEAPIToken={token_id}={secret}"}


def pbs_headers(token_id: str, secret: str) -> dict:
    # PBS joins token id and secret with a COLON, unlike PVE's '='
    return {"Authorization": f"PBSAPIToken={token_id}:{secret}"}


def unifi_headers(key: str) -> dict:
    return {"X-API-KEY": key, "Accept": "application/json"}


async def api_get(url: str, headers: dict, timeout: float):
    """GET via the shared client; unwrap the {"data": ...} envelope that PVE,
    PBS, and the UniFi Integration API all use. Raises on non-2xx."""
    r = await client().get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


# --- TrueNAS: JSON-RPC 2.0 over WebSocket -------------------------------------
#
# The legacy REST API (/api/v2.0) is deprecated as of SCALE 25.04 and removed
# in 26. TLS is mandatory: TrueNAS revokes any API key presented over plain ws.

class TrueNasAuthError(RuntimeError):
    """The API key was rejected at login."""


class TrueNasRpc:
    """Minimal JSON-RPC 2.0 caller over an open websocket connection."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0

    async def call(self, method: str, params: "list | None" = None):
        self._id += 1
        req_id = self._id
        await self.ws.send(json.dumps({
            "jsonrpc": "2.0", "id": req_id, "method": method, "params": params or [],
        }))
        # Skip event notifications until we see the reply matching our id.
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result")


@asynccontextmanager
async def truenas_session(host: str, key: str, timeout: float):
    """Connect, authenticate, and yield an RPC caller. The timeout covers the
    whole session (connect + every call), mirroring the old per-fetch bound."""
    uri = f"wss://{host}/api/current"
    async with asyncio.timeout(timeout * 2):
        async with websockets.connect(uri, ssl=insecure_ssl_context()) as ws:
            rpc = TrueNasRpc(ws)
            if not await rpc.call("auth.login_with_api_key", [key]):
                raise TrueNasAuthError("API key rejected")
            yield rpc


# --- UniFi discovery ------------------------------------------------------------

GW_KEYWORDS = ("dream machine", "gateway", "uxg", "ucg", "udr", "udw", "udm")


def pick_gateway(devices: list) -> "dict | None":
    return next(
        (d for d in devices
         if any(k in (d.get("model", "") + d.get("name", "")).lower()
                for k in GW_KEYWORDS)),
        devices[0] if devices else None,
    )


async def unifi_site_id(host: str, key: str, site: str, timeout: float) -> str:
    """Resolve the opaque site id the Integration API wants (the legacy
    "default" slug is not valid); fall back to the first site."""
    sites = await api_get(f"{unifi_base(host)}/sites", unifi_headers(key), timeout) or []
    if site and site != "default":
        for s in sites:
            if site in (s.get("name"), s.get("id")):
                return s["id"]
    return sites[0]["id"] if sites else site
