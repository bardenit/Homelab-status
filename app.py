"""
Homelab status aggregator.

Polls Proxmox VE, TrueNAS, and Proxmox Backup Server, then exposes one small,
flat JSON payload at /api/status for the ESP32-2432S028R (Cheap Yellow Display)
to render. All the TLS / token-auth / fat-JSON-parsing pain lives here so the
microcontroller never has to deal with it.

Config is entirely via environment variables (see .env.example). Nothing is
hardcoded. Any single source failing degrades gracefully: that source is marked
err=true and the rest of the payload still returns.

Set MOCK=1 to serve fake data with no backends (handy for flashing/testing the
CYD before you wire in real credentials).
"""

import asyncio
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI

# --- config ----------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        return default

MOCK = _env("MOCK") in ("1", "true", "yes")

# Proxmox VE: host like "10.0.0.10:8006", token "user@pam!tokenid=SECRET-UUID"
PVE_HOST = _env("PVE_HOST")
PVE_TOKEN = _env("PVE_TOKEN")

# TrueNAS: host like "10.0.0.20", key is the raw API key string
TRUENAS_HOST = _env("TRUENAS_HOST")
TRUENAS_KEY = _env("TRUENAS_KEY")

# PBS: host like "10.0.0.30:8007", token "user@pbs!tokenid:SECRET"
# NOTE the PBS token separator is a COLON, unlike PVE which uses '='.
PBS_HOST = _env("PBS_HOST")
PBS_TOKEN = _env("PBS_TOKEN")
PBS_NODE = _env("PBS_NODE", "localhost")  # node name used in the PBS task log path

# Alert thresholds (percent)
MEM_WARN = _envf("MEM_WARN", 90)
POOL_WARN = _envf("POOL_WARN", 85)
PBS_WARN = _envf("PBS_WARN", 85)

CACHE_TTL = _envf("CACHE_TTL", 10)        # seconds; the CYD can poll faster than this safely
HTTP_TIMEOUT = _envf("HTTP_TIMEOUT", 6)   # per-request timeout

# --- helpers ---------------------------------------------------------------

def pct(used: float, total: float) -> int:
    if not total:
        return 0
    return max(0, min(100, round(used / total * 100)))


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> Any:
    r = await client.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# --- source: Proxmox VE -----------------------------------------------------

async def fetch_pve(client: httpx.AsyncClient) -> dict:
    out: dict = {"nodes": [], "quorate": None, "err": False}
    if not (PVE_HOST and PVE_TOKEN):
        out["err"] = True
        return out
    base = f"https://{PVE_HOST}/api2/json"
    headers = {"Authorization": f"PVEAPIToken={PVE_TOKEN}"}
    try:
        # quorum
        status = (await _get(client, f"{base}/cluster/status", headers)).get("data", [])
        for item in status:
            if item.get("type") == "cluster":
                out["quorate"] = bool(item.get("quorate"))
                break

        # per-node resources in one shot
        res = (await _get(client, f"{base}/cluster/resources?type=node", headers)).get("data", [])
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

async def fetch_truenas(client: httpx.AsyncClient) -> dict:
    out: dict = {"pools": [], "err": False}
    if not (TRUENAS_HOST and TRUENAS_KEY):
        out["err"] = True
        return out
    base = f"https://{TRUENAS_HOST}/api/v2.0"
    headers = {"Authorization": f"Bearer {TRUENAS_KEY}"}
    try:
        pools = await _get(client, f"{base}/pool", headers)
        for p in pools:
            name = p.get("name", "?")
            status = p.get("status", "UNKNOWN")
            healthy = p.get("healthy", status == "ONLINE")

            # Capacity: pool object may carry size/allocated on some versions.
            # If not present, fall back to the root dataset usage, which is
            # stable across SCALE releases.
            size = p.get("size")
            alloc = p.get("allocated")
            if size and alloc is not None:
                used = pct(alloc, size)
            else:
                used = 0
                try:
                    ds = await _get(client, f"{base}/pool/dataset/id/{name}", headers)
                    u = ds.get("used", {}).get("parsed", 0)
                    a = ds.get("available", {}).get("parsed", 0)
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

async def fetch_pbs(client: httpx.AsyncClient) -> dict:
    out: dict = {"datastores": [], "err": False}
    if not (PBS_HOST and PBS_TOKEN):
        out["err"] = True
        return out
    base = f"https://{PBS_HOST}/api2/json"
    headers = {"Authorization": f"PBSAPIToken={PBS_TOKEN}"}
    try:
        usage = (await _get(client, f"{base}/status/datastore-usage", headers)).get("data", [])

        # Best-effort: most recent garbage-collection task end time, for a
        # "last GC age" readout. Failure here must not drop the usage data.
        gc_end = None
        try:
            tasks = (await _get(
                client,
                f"{base}/nodes/{PBS_NODE}/tasks?typefilter=garbage_collection&limit=1",
                headers,
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


def compute_alert(payload: dict) -> bool:
    if payload.get("quorate") is False:
        return True
    if any(not n["up"] for n in payload["nodes"]):
        return True
    if any(n["up"] and n["mem"] >= MEM_WARN for n in payload["nodes"]):
        return True
    if any(not p["ok"] for p in payload["pools"]):
        return True
    if any(p["used"] >= POOL_WARN for p in payload["pools"]):
        return True
    if any(d["used"] >= PBS_WARN for d in payload["pbs"]):
        return True
    if not all(payload["sources"].values()):
        return True
    return False


async def build_payload() -> dict:
    if MOCK:
        return mock_payload()

    # one client, self-signed certs are the norm on these boxes
    async with httpx.AsyncClient(verify=False) as client:
        pve, tn, pbs = await asyncio.gather(
            fetch_pve(client), fetch_truenas(client), fetch_pbs(client)
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
    payload["alert"] = compute_alert(payload)
    return payload


async def get_cached() -> dict:
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    async with _lock:
        # re-check after acquiring the lock to avoid a thundering herd
        if _cache["data"] is not None and (time.time() - _cache["ts"]) < CACHE_TTL:
            return _cache["data"]
        data = await build_payload()
        _cache["data"] = data
        _cache["ts"] = time.time()
        return data


# --- app --------------------------------------------------------------------

app = FastAPI(title="Homelab Panel Aggregator")


@app.get("/api/status")
async def status():
    return await get_cached()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mock": MOCK}
