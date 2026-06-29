"""
Homelab status aggregator.

Polls Proxmox VE, TrueNAS, and Proxmox Backup Server, then exposes one small,
flat JSON payload at /api/status for the ESP32-2432S028R (Cheap Yellow Display)
to render. All the TLS / token-auth / fat-JSON-parsing pain lives here so the
microcontroller never has to deal with it.

Config is loaded by config.py: the admin UI writes /data/config.json, which
overlays environment variables, which overlay built-in defaults. Settings can
be changed at runtime via /admin (no restart). Any single source failing
degrades gracefully: that source is marked err=true and the rest still returns.

Set mock mode (env MOCK=1 or the admin toggle) to serve fake data with no
backends, handy for flashing/testing the CYD before wiring in real credentials.
"""

import asyncio
import json
import ssl
import time
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware

import config
import firmware

# --- helpers ---------------------------------------------------------------

def pct(used: float, total: float) -> int:
    if not total:
        return 0
    return max(0, min(100, round(used / total * 100)))


async def _get(client: httpx.AsyncClient, url: str, headers: dict, timeout: float) -> Any:
    r = await client.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


# --- source: Proxmox VE -----------------------------------------------------

async def fetch_pve(client: httpx.AsyncClient, cfg: config.Config) -> dict:
    out: dict = {"nodes": [], "quorate": None, "err": False}
    if not (cfg.pve_host and cfg.pve_token_id and cfg.pve_secret):
        out["err"] = True
        return out
    base = f"https://{cfg.pve_host}/api2/json"
    headers = {"Authorization": f"PVEAPIToken={cfg.pve_token_id}={cfg.pve_secret}"}
    try:
        # quorum
        status = (await _get(client, f"{base}/cluster/status", headers, cfg.http_timeout)).get("data", [])
        for item in status:
            if item.get("type") == "cluster":
                out["quorate"] = bool(item.get("quorate"))
                break

        # per-node resources in one shot
        res = (await _get(client, f"{base}/cluster/resources?type=node", headers, cfg.http_timeout)).get("data", [])
        for n in sorted(res, key=lambda x: x.get("node", "")):
            up = n.get("status") == "online"
            out["nodes"].append({
                "name": n.get("node", "?"),
                "up": up,
                "cpu": round((n.get("cpu") or 0) * 100) if up else 0,
                "mem": pct(n.get("mem", 0), n.get("maxmem", 0)) if up else 0,
            })
    except Exception:
        out["err"] = True
    return out


# --- source: TrueNAS --------------------------------------------------------
#
# TrueNAS uses the JSON-RPC 2.0 over WebSocket API (wss://host/api/current).
# The legacy REST API (/api/v2.0) is deprecated as of SCALE 25.04 and removed
# in 26. TLS is mandatory: TrueNAS revokes any API key presented over plain ws.

class _RpcConn:
    """Minimal JSON-RPC 2.0 caller over an open websocket connection."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._id = 0

    async def call(self, method: str, params: list | None = None) -> Any:
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


async def fetch_truenas(client: httpx.AsyncClient, cfg: config.Config) -> dict:
    # client (httpx) is unused here: TrueNAS speaks JSON-RPC over websocket now.
    out: dict = {"pools": [], "err": False}
    if not (cfg.truenas_host and cfg.truenas_key):
        out["err"] = True
        return out
    uri = f"wss://{cfg.truenas_host}/api/current"
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with asyncio.timeout(cfg.http_timeout * 2):
            async with websockets.connect(uri, ssl=ssl_ctx) as ws:
                rpc = _RpcConn(ws)
                if not await rpc.call("auth.login_with_api_key", [cfg.truenas_key]):
                    out["err"] = True
                    return out

                pools = await rpc.call("pool.query")
                for p in pools:
                    name = p.get("name", "?")
                    status = p.get("status", "UNKNOWN")
                    healthy = p.get("healthy", status == "ONLINE")

                    # Capacity: pool object may carry size/allocated on some
                    # versions. If not present, fall back to the root dataset
                    # usage, which is stable across SCALE releases.
                    size = p.get("size")
                    alloc = p.get("allocated")
                    if size and alloc is not None:
                        used = pct(alloc, size)
                    else:
                        used = 0
                        try:
                            ds = await rpc.call("pool.dataset.query", [[["id", "=", name]]])
                            if ds:
                                u = ds[0].get("used", {}).get("parsed", 0)
                                a = ds[0].get("available", {}).get("parsed", 0)
                                used = pct(u, u + a)
                        except Exception:
                            pass

                    out["pools"].append({
                        "name": name,
                        "health": status,
                        "ok": bool(healthy) and status == "ONLINE",
                        "used": used,
                    })
    except Exception:
        out["err"] = True
    return out


# --- source: PBS ------------------------------------------------------------

async def fetch_pbs(client: httpx.AsyncClient, cfg: config.Config) -> dict:
    out: dict = {"datastores": [], "err": False}
    if not (cfg.pbs_host and cfg.pbs_token_id and cfg.pbs_secret):
        out["err"] = True
        return out
    base = f"https://{cfg.pbs_host}/api2/json"
    headers = {"Authorization": f"PBSAPIToken={cfg.pbs_token_id}:{cfg.pbs_secret}"}
    try:
        usage = (await _get(client, f"{base}/status/datastore-usage", headers, cfg.http_timeout)).get("data", [])

        # Best-effort: most recent garbage-collection task end time, for a
        # "last GC age" readout. Failure here must not drop the usage data.
        gc_end = None
        try:
            tasks = (await _get(
                client,
                f"{base}/nodes/{cfg.pbs_node}/tasks?typefilter=garbage_collection&limit=1",
                headers,
                cfg.http_timeout,
            )).get("data", [])
            if tasks:
                gc_end = tasks[0].get("endtime")
        except Exception:
            pass

        for d in usage:
            total = d.get("total", 0)
            used = d.get("used", 0)
            gc_age_h = None
            if gc_end:
                gc_age_h = round((time.time() - gc_end) / 3600, 1)
            out["datastores"].append({
                "name": d.get("store", "?"),
                "used": pct(used, total),
                "gc_age_h": gc_age_h,
            })
    except Exception:
        out["err"] = True
    return out


# --- mock -------------------------------------------------------------------

def mock_payload() -> dict:
    return {
        "ts": int(time.time()),
        "quorate": True,
        "nodes": [
            {"name": "mox-i9", "up": True, "cpu": 14, "mem": 61},
            {"name": "mox-i7", "up": True, "cpu": 8, "mem": 47},
            {"name": "mox-i5", "up": True, "cpu": 22, "mem": 73},
            {"name": "mox-n1", "up": True, "cpu": 3, "mem": 31},
            {"name": "mox-n2", "up": False, "cpu": 0, "mem": 0},
        ],
        "pools": [
            {"name": "SSD_Pool", "health": "ONLINE", "ok": True, "used": 58},
            {"name": "nvme_pool", "health": "ONLINE", "ok": True, "used": 71},
            {"name": "Secure_Pool", "health": "DEGRADED", "ok": False, "used": 44},
            {"name": "TV-Pool", "health": "ONLINE", "ok": True, "used": 88},
        ],
        "pbs": [
            {"name": "main", "used": 82, "gc_age_h": 36.5},
        ],
        "sources": {"pve": True, "truenas": True, "pbs": True},
        "alert": True,
    }


# --- assembly + cache -------------------------------------------------------

_cache: dict = {"ts": 0, "data": None}
_lock = asyncio.Lock()


def invalidate_cache() -> None:
    """Force the next /api/status to rebuild (called after a config save)."""
    _cache["ts"] = 0


def compute_alert(payload: dict, cfg: config.Config) -> bool:
    if payload.get("quorate") is False:
        return True
    if any(not n["up"] for n in payload["nodes"]):
        return True
    if any(n["up"] and n["mem"] >= cfg.mem_warn for n in payload["nodes"]):
        return True
    if any(not p["ok"] for p in payload["pools"]):
        return True
    if any(p["used"] >= cfg.pool_warn for p in payload["pools"]):
        return True
    if any(d["used"] >= cfg.pbs_warn for d in payload["pbs"]):
        return True
    if not all(payload["sources"].values()):
        return True
    return False


async def build_payload() -> dict:
    cfg = config.get()
    if cfg.mock:
        return mock_payload()

    # one client, self-signed certs are the norm on these boxes
    async with httpx.AsyncClient(verify=False) as client:
        pve, tn, pbs = await asyncio.gather(
            fetch_pve(client, cfg), fetch_truenas(client, cfg), fetch_pbs(client, cfg)
        )

    payload = {
        "ts": int(time.time()),
        "quorate": pve["quorate"],
        "nodes": pve["nodes"],
        "pools": tn["pools"],
        "pbs": pbs["datastores"],
        "sources": {
            "pve": not pve["err"],
            "truenas": not tn["err"],
            "pbs": not pbs["err"],
        },
    }
    payload["alert"] = compute_alert(payload, cfg)
    return payload


async def get_cached() -> dict:
    cfg = config.get()
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < cfg.cache_ttl:
        return _cache["data"]
    async with _lock:
        # re-check after acquiring the lock to avoid a thundering herd
        if _cache["data"] is not None and (time.time() - _cache["ts"]) < cfg.cache_ttl:
            return _cache["data"]
        data = await build_payload()
        _cache["data"] = data
        _cache["ts"] = time.time()
        return data


# --- app --------------------------------------------------------------------

_cfg = config.load()

app = FastAPI(title="Homelab Panel Aggregator")
app.add_middleware(
    SessionMiddleware,
    secret_key=_cfg.session_secret,
    same_site="lax",
    https_only=False,
)

from admin import router as admin_router  # noqa: E402  (after app/config are ready)

app.include_router(admin_router)


@app.get("/api/status")
async def status(request: Request):
    # Optional panel self-report (Phase 3 firmware sends these), so the admin
    # UI can show which panel is on which firmware. Never required.
    panel_id = request.headers.get("x-panel-id") or request.query_params.get("id")
    if panel_id:
        version = request.headers.get("x-panel-version") or request.query_params.get("fw") or "?"
        ip = request.client.host if request.client else "?"
        firmware.record_checkin(panel_id, version, ip)
    return await get_cached()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mock": config.get().mock}


# --- firmware / OTA (public; panels cannot authenticate) --------------------

_manifest_hits: dict = {}  # client ip -> last fetch epoch


@app.get("/firmware/manifest.json")
async def firmware_manifest(request: Request):
    # Rate-limit manifest polls per IP to blunt a misconfigured poll storm.
    # The bin download below is deliberately NOT limited, so OTA + retries
    # stay reliable.
    ip = request.client.host if request.client else "?"
    now = time.time()
    if now - _manifest_hits.get(ip, 0) < config.get().fw_min_interval:
        return Response(status_code=429)
    _manifest_hits[ip] = now

    m = firmware.manifest()
    if m is None:
        return Response(status_code=404)
    return JSONResponse(m)


@app.get("/firmware/firmware.ota.bin")
async def firmware_bin():
    if not firmware.FW_BIN.exists():
        return Response(status_code=404)
    return FileResponse(
        str(firmware.FW_BIN),
        media_type="application/octet-stream",
        filename=firmware.OTA_FILENAME,
    )
