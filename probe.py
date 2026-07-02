"""
Connection probes for the admin "Test" buttons.

Unlike the fetch_* functions in app.py (which deliberately swallow errors and
just mark a source false), these surface the real reason a backend call failed
(401 vs 403 vs connection error) so the admin form can show actionable text.

They test whatever values are passed in, which is how the UI tests unsaved,
just-typed credentials rather than what is persisted. Each probe uses a fresh
short-lived client on purpose: a Test should exercise a cold connection, not
whatever the shared pool already has open.
"""

import httpx

import backends


async def _request(url: str, headers: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(verify=False) as client:
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
            f"{backends.pve_base(host)}/cluster/status",
            backends.pve_headers(token_id, secret),
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
    # JSON-RPC over WebSocket via backends.truenas_session (REST is deprecated
    # in SCALE 25.04 and removed in 26; keys sent over plain ws get revoked).
    miss = _need(host=host, key=key)
    if miss:
        return {"ok": False, "status": None, "detail": miss}
    try:
        async with backends.truenas_session(host, key, timeout) as rpc:
            try:
                pools = await rpc.call("pool.query")
            except RuntimeError as e:
                return {
                    "ok": False,
                    "status": 403,
                    "detail": f"Authenticated, but pool.query was denied: {e}. The key's user needs read access to pools.",
                }
            return {"ok": True, "status": 200, "detail": f"Connected — {len(pools)} pool(s)."}
    except backends.TrueNasAuthError:
        return {
            "ok": False,
            "status": 403,
            "detail": "API key rejected. Check the key, and that its user has the Readonly Admin (or higher) role.",
        }
    except Exception as e:
        return _conn_error(e)


async def probe_unifi(host: str, key: str, site: str, timeout: float) -> dict:
    # UniFi OS Integration API: X-API-KEY header, base /proxy/network/integration/v1.
    miss = _need(host=host, key=key)
    if miss:
        return {"ok": False, "status": None, "detail": miss}
    # The Integration API uses opaque site IDs from GET /sites (not the legacy
    # "default" slug), so validate the key against /sites, which needs no site.
    try:
        r = await _request(f"{backends.unifi_base(host)}/sites",
                           backends.unifi_headers(key), timeout)
    except Exception as e:
        return _conn_error(e)
    if r.status_code == 401:
        return {"ok": False, "status": 401, "detail": "401 — API key rejected. Regenerate it in UniFi (Settings, Control Plane, Integrations)."}
    if r.status_code == 404:
        return {"ok": False, "status": 404, "detail": "404 — Integration API not found (needs Network app 10.1.84+)."}
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "detail": _explain(r.status_code) + " (check host is the UDM console)."}
    try:
        body = r.json()
        sites = body.get("data", body) if isinstance(body, dict) else body
        names = ", ".join(str(s.get("name") or s.get("id")) for s in sites[:4])
        return {"ok": True, "status": 200, "detail": f"Connected — {len(sites)} site(s): {names}"}
    except Exception:
        return {"ok": True, "status": 200, "detail": "Connected (HTTP 200)."}


async def probe_pbs(host: str, token_id: str, secret: str, timeout: float) -> dict:
    miss = _need(host=host, token_id=token_id, secret=secret)
    if miss:
        return {"ok": False, "status": None, "detail": miss}
    try:
        r = await _request(
            f"{backends.pbs_base(host)}/status/datastore-usage",
            backends.pbs_headers(token_id, secret),
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
