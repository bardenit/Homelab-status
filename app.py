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
import logging
import random
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware

import backends
import config
import firmware
import ui
from backends import api_get, pct

log = logging.getLogger("aggregator")


# --- source: Proxmox VE -----------------------------------------------------

async def fetch_pve(cfg: config.Config) -> dict:
    out: dict = {"nodes": [], "quorate": None, "err": False}
    if not (cfg.pve_host and cfg.pve_token_id and cfg.pve_secret):
        out["err"] = True
        return out
    base = backends.pve_base(cfg.pve_host)
    headers = backends.pve_headers(cfg.pve_token_id, cfg.pve_secret)
    try:
        # quorum
        status = await api_get(f"{base}/cluster/status", headers, cfg.http_timeout) or []
        for item in status:
            if item.get("type") == "cluster":
                out["quorate"] = bool(item.get("quorate"))
                break

        # per-node resources in one shot
        res = await api_get(f"{base}/cluster/resources?type=node", headers, cfg.http_timeout) or []
        for n in sorted(res, key=lambda x: x.get("node", "")):
            up = n.get("status") == "online"
            out["nodes"].append({
                "name": n.get("node", "?"),
                "up": up,
                "cpu": round((n.get("cpu") or 0) * 100) if up else 0,
                "mem": pct(n.get("mem", 0), n.get("maxmem", 0)) if up else 0,
                "drill": f"pve/node/{quote(str(n.get('node', '')), safe='')}",
            })
    except Exception:
        out["err"] = True
    return out


# --- source: TrueNAS (JSON-RPC over WebSocket, see backends.truenas_session) --

async def fetch_truenas(cfg: config.Config) -> dict:
    out: dict = {"pools": [], "err": False}
    if not (cfg.truenas_host and cfg.truenas_key):
        out["err"] = True
        return out
    try:
        async with backends.truenas_session(
            cfg.truenas_host, cfg.truenas_key, cfg.http_timeout
        ) as rpc:
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
                    "drill": f"truenas/pool/{quote(str(name), safe='')}",
                })
    except Exception:
        out["err"] = True
    return out


# --- source: PBS ------------------------------------------------------------

async def fetch_pbs(cfg: config.Config) -> dict:
    out: dict = {"datastores": [], "err": False}
    if not (cfg.pbs_host and cfg.pbs_token_id and cfg.pbs_secret):
        out["err"] = True
        return out
    base = backends.pbs_base(cfg.pbs_host)
    headers = backends.pbs_headers(cfg.pbs_token_id, cfg.pbs_secret)
    try:
        usage = await api_get(f"{base}/status/datastore-usage", headers, cfg.http_timeout) or []

        # Resolve the node name for the task-log path. The configured value (often
        # "localhost") may not match PBS's real node name, so discover it and fall
        # back to the configured value.
        node = cfg.pbs_node
        try:
            nodes = await api_get(f"{base}/nodes", headers, cfg.http_timeout) or []
            if nodes:
                node = nodes[0].get("node") or node
        except Exception as e:
            log.warning("PBS node discovery failed (%s); using configured node %r", e, cfg.pbs_node)

        for d in usage:
            store = d.get("store", "?")
            total = d.get("total", 0)
            used = d.get("used", 0)

            # Most recent finished GC for THIS datastore. Scope by store so a
            # Datastore.Audit token (not the task owner, and without Sys.Audit) is
            # still allowed to see it, and so each datastore reports its own GC.
            gc_age_h = None
            try:
                url = (f"{base}/nodes/{node}/tasks"
                       f"?store={store}&typefilter=garbage_collection&limit=1")
                tasks = await api_get(url, headers, cfg.http_timeout) or []
                gc_end = tasks[0].get("endtime") if tasks else None
                if gc_end:
                    gc_age_h = round((time.time() - gc_end) / 3600, 1)
                else:
                    log.warning("PBS GC lookup for %r on node %r returned no tasks", store, node)
            except Exception as e:
                log.warning("PBS GC lookup for %r on node %r failed: %s", store, node, e)

            out["datastores"].append({
                "name": store,
                "used": pct(used, total),
                "gc_age_h": gc_age_h,
                "drill": f"pbs/datastore/{quote(str(store), safe='')}",
            })
    except Exception:
        out["err"] = True
    return out


# --- source: UniFi ----------------------------------------------------------

# Memoized IDs + slow-changing counts so the fast path is a single API call
# (the gateway's statistics/latest). Site/gateway are re-resolved rarely; the
# device/client counts refresh on a slower cadence than the live throughput.
_unifi_ids: dict = {"site": None, "gw": None, "ts": 0.0}
_unifi_counts: dict = {"clients": 0, "dev_online": 0, "dev_total": 0, "ts": 0.0}
_unifi_fast_cache: dict = {"ts": 0.0, "data": None}

UNIFI_FAST_TTL = 1.0     # dedupe concurrent panel polls
UNIFI_ID_TTL = 300.0     # re-resolve site/gateway ids every 5 min
UNIFI_COUNT_TTL = 15.0   # refresh device/client counts every 15s


async def fetch_unifi_fast(cfg: config.Config) -> dict:
    out: dict = {"wan_down": 0.0, "wan_up": 0.0, "cpu": 0, "mem": 0,
                 "clients": _unifi_counts["clients"],
                 "dev_online": _unifi_counts["dev_online"],
                 "dev_total": _unifi_counts["dev_total"], "err": False}
    if not (cfg.unifi_host and cfg.unifi_key):
        out["err"] = True
        return out
    base = backends.unifi_base(cfg.unifi_host)
    headers = backends.unifi_headers(cfg.unifi_key)
    now = time.time()
    try:
        # (re)resolve site + gateway id, and refresh counts, only when stale
        if _unifi_ids["gw"] is None or (now - _unifi_ids["ts"]) > UNIFI_ID_TTL \
                or (now - _unifi_counts["ts"]) > UNIFI_COUNT_TTL:
            site = await backends.unifi_site_id(
                cfg.unifi_host, cfg.unifi_key, cfg.unifi_site, cfg.http_timeout)
            devices = await api_get(f"{base}/sites/{site}/devices", headers, cfg.http_timeout) or []
            gw = backends.pick_gateway(devices)
            _unifi_ids.update(site=site, gw=(gw or {}).get("id"), ts=now)
            clients = await api_get(f"{base}/sites/{site}/clients", headers, cfg.http_timeout) or []
            _unifi_counts.update(clients=len(clients),
                                 dev_online=sum(1 for d in devices if d.get("state") == "ONLINE"),
                                 dev_total=len(devices), ts=now)
            out.update(clients=_unifi_counts["clients"],
                       dev_online=_unifi_counts["dev_online"],
                       dev_total=_unifi_counts["dev_total"])

        # the live call: gateway throughput + load (one request)
        if _unifi_ids["gw"]:
            st = await api_get(
                f"{base}/sites/{_unifi_ids['site']}/devices/{_unifi_ids['gw']}/statistics/latest",
                headers, cfg.http_timeout) or {}
            up = st.get("uplink") or {}
            out["wan_down"] = round(float(up.get("rxRateBps", 0)) * 8 / 1_000_000, 1)
            out["wan_up"] = round(float(up.get("txRateBps", 0)) * 8 / 1_000_000, 1)
            out["cpu"] = round(st.get("cpuUtilizationPct") or 0)
            out["mem"] = round(st.get("memoryUtilizationPct") or 0)
    except Exception:
        out["err"] = True
        _unifi_ids["gw"] = None  # force re-resolve next time
    return out


async def get_unifi_cached() -> dict:
    cfg = config.get()
    if cfg.mock:
        return mock_unifi()
    now = time.time()
    if _unifi_fast_cache["data"] is not None and (now - _unifi_fast_cache["ts"]) < UNIFI_FAST_TTL:
        return _unifi_fast_cache["data"]
    data = await fetch_unifi_fast(cfg)
    _unifi_fast_cache.update(ts=now, data=data)
    return data


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


def mock_unifi() -> dict:
    return {"wan_down": round(random.uniform(20, 120), 1),
            "wan_up": round(random.uniform(5, 40), 1),
            "cpu": random.randint(15, 45), "mem": 60,
            "clients": 25, "dev_online": 11, "dev_total": 12, "err": False}


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

    # UniFi is NOT here: it has its own fast /api/unifi lane so the panel can
    # poll it quickly without dragging the heavy PVE/TrueNAS/PBS sweep along.
    pve, tn, pbs = await asyncio.gather(
        fetch_pve(cfg), fetch_truenas(cfg), fetch_pbs(cfg)
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await backends.close_client()


app = FastAPI(title="Homelab Panel Aggregator", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=_cfg.session_secret,
    same_site="lax",
    https_only=False,
)

from admin import router as admin_router  # noqa: E402  (after app/config are ready)

app.include_router(admin_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/admin"):
        # never let a browser or proxy cache admin pages (config forms, etc.);
        # CSP is pragmatic (the config page uses one inline script) but still
        # pins all fetches/forms to this origin and bans framing/objects
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
    return resp


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


@app.get("/api/unifi")
async def api_unifi():
    # Fast lane: WAN throughput + gateway load, polled at ~1s by the panel.
    return await get_unifi_cached()


@app.get("/healthz")
async def healthz():
    # config: "error" means /data/config.json exists but could not be read
    # (bad volume perms / corrupt JSON) and the app fell back to env defaults.
    return {
        "ok": True,
        "mock": config.get().mock,
        "config": "error" if config.load_error else "ok",
    }


# --- navigable drill-down screens (public; the panel browses these) ---------

@app.get("/ui")
@app.get("/ui/{path:path}")
async def ui_screen(path: str = ""):
    return await ui.screen(path)


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
    # drop stale entries so the map cannot grow without bound
    for stale in [k for k, ts in _manifest_hits.items() if now - ts > 3600]:
        del _manifest_hits[stale]
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
