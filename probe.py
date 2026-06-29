"""
Connection probes for the admin "Test" buttons.

Unlike the fetch_* functions in app.py (which deliberately swallow errors and
just mark a source false), these surface the real reason a backend call failed
(401 vs 403 vs connection error) so the admin form can show actionable text.

They test whatever values are passed in, which is how the UI tests unsaved,
just-typed credentials rather than what is persisted.
"""

import asyncio
import json
import ssl

import httpx
import websockets

VERIFY_SSL = False  # self-signed certs are the norm on these boxes


async def _request(url: str, headers: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(verify=VERIFY_SSL) as client:
        return await client.get(url, headers=headers, timeout=timeout)


def _need(**fields) -> "str | None":
    missing = [k.replace("_", " ") for k, v in fields.items() if not (v or "").strip()]
    return ("Fill in " + ", ".join(missing) + " first.") if missing else None


def _explain(status: int) -> str:
    return {
        400: "400 Bad Request — check the host and token format.",
        401: "401 Unauthorized — token ID (case-sensitive) or secret is wrong.",
        403: "403 Forbidden — token authenticates but lacks read permission.",
        404: "404 Not Found — wrong host/port or API path.",
        500: "500 — the backend returned a server error.",
    }.get(status, f"HTTP {status}.")


def _conn_error(exc: Exception) -> dict:
    return {
        "ok": False,
        "status": None,
        "detail": f"Could not connect ({type(exc).__name__}). Check the host, port, and network.",
    }


async def probe_pve(host: str, token_id: str, secret: str, timeout: float) -> dict:
    miss = _need(host=host, token_id=token_id, secret=secret)
    if miss:
        return {"ok": False, "status": None, "detail": miss}
    try:
        r = await _request(
            f"https://{host}/api2/json/cluster/status",
            {"Authorization": f"PVEAPIToken={token_id}={secret}"},
            timeout,
        )
    except Exception as e:
        return _conn_error(e)
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "detail": _explain(r.status_code)}
    try:
        data = r.json().get("data", [])
        nodes = [d for d in data if d.get("type") == "node"]
        quorate = next((d.get("quorate") for d in data if d.get("type") == "cluster"), None)
        bits = []
        if quorate is not None:
            bits.append("quorum " + ("OK" if quorate else "LOST"))
        bits.append(f"{len(nodes)} node(s)")
        return {"ok": True, "status": 200, "detail": "Connected — " + ", ".join(bits) + "."}
    except Exception:
        return {"ok": True, "status": 200, "detail": "Connected (HTTP 200)."}


async def probe_truenas(host: str, key: str, timeout: float) -> dict:
    # TrueNAS uses JSON-RPC 2.0 over WebSocket (the REST API is deprecated in
    # SCALE 25.04 and removed in 26). TLS is mandatory: keys sent over plain ws
    # get revoked. This mirrors the handshake in app.fetch_truenas.
    miss = _need(host=host, key=key)
    if miss:
        return {"ok": False, "status": None, "detail": miss}
    uri = f"wss://{host}/api/current"
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with asyncio.timeout(timeout * 2):
            async with websockets.connect(uri, ssl=ssl_ctx) as ws:
                rid = 0

                async def call(method, params=None):
                    nonlocal rid
                    rid += 1
                    my_id = rid
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": my_id, "method": method, "params": params or [],
                    }))
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") != my_id:
                            continue
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result")

                if not await call("auth.login_with_api_key", [key]):
                    return {
                        "ok": False,
                        "status": 403,
                        "detail": "API key rejected. Check the key, and that its user has the Readonly Admin (or higher) role.",
                    }
                try:
                    pools = await call("pool.query")
                except RuntimeError as e:
                    return {
                        "ok": False,
                        "status": 403,
                        "detail": f"Authenticated, but pool.query was denied: {e}. The key's user needs read access to pools.",
                    }
                return {"ok": True, "status": 200, "detail": f"Connected — {len(pools)} pool(s)."}
    except Exception as e:
        return _conn_error(e)


async def probe_pbs(host: str, token_id: str, secret: str, timeout: float) -> dict:
    miss = _need(host=host, token_id=token_id, secret=secret)
    if miss:
        return {"ok": False, "status": None, "detail": miss}
    try:
        r = await _request(
            f"https://{host}/api2/json/status/datastore-usage",
            {"Authorization": f"PBSAPIToken={token_id}:{secret}"},
            timeout,
        )
    except Exception as e:
        return _conn_error(e)
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "detail": _explain(r.status_code)}
    try:
        ds = r.json().get("data", [])
        return {"ok": True, "status": 200, "detail": f"Connected — {len(ds)} datastore(s)."}
    except Exception:
        return {"ok": True, "status": 200, "detail": "Connected (HTTP 200)."}
