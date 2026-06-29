"""
Firmware hosting for the CYD panels (pull-based OTA).

The admin uploads an ESPHome OTA binary; it is stored in the /data volume with
its md5/size/version, and served via an ESPHome-compatible update manifest
(the `update: platform: http_request` schema). Panels poll the manifest and
self-update over WiFi.

Also records per-panel version check-ins (fed from the /api/status poll) so the
admin UI can show which panel is running which firmware.

One firmware for all panels: they are identical CYDs running the same build.
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("CONFIG_PATH", "/data/config.json")).parent
FW_DIR = DATA_DIR / "firmware"
FW_BIN = FW_DIR / "firmware.ota.bin"
FW_META = FW_DIR / "meta.json"
DEVICES_PATH = DATA_DIR / "devices.json"

CHIP_FAMILY = "ESP32"          # ESP32-D0WD on the 2432S028R
PROJECT_NAME = "homelab-panel"
OTA_FILENAME = "firmware.ota.bin"   # path is relative to the manifest URL


# --- atomic writers ---------------------------------------------------------

def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, obj) -> None:
    _atomic_write_bytes(path, json.dumps(obj, indent=2).encode())


# --- firmware storage -------------------------------------------------------

def save_firmware(data: bytes, version: str, summary: str = "") -> dict:
    meta = {
        "version": version,
        "md5": hashlib.md5(data).hexdigest(),
        "size": len(data),
        "summary": summary,
        "uploaded_at": int(time.time()),
    }
    _atomic_write_bytes(FW_BIN, data)
    _atomic_write_json(FW_META, meta)
    return meta


def get_meta() -> "dict | None":
    if FW_META.exists() and FW_BIN.exists():
        try:
            return json.loads(FW_META.read_text())
        except Exception:
            return None
    return None


def manifest() -> "dict | None":
    """ESPHome `update: platform: http_request` manifest, or None if no upload."""
    meta = get_meta()
    if not meta:
        return None
    return {
        "name": PROJECT_NAME,
        "version": meta["version"],
        "home_assistant_domain": "esphome",
        "new_install_prompt_erase": False,
        "builds": [
            {
                "chipFamily": CHIP_FAMILY,
                "ota": {
                    "md5": meta["md5"],
                    "path": OTA_FILENAME,
                    "summary": meta.get("summary", ""),
                },
            }
        ],
    }


# --- per-panel version check-ins --------------------------------------------
# Kept in memory and flushed to disk on change or every 30s, so the frequent
# /api/status polls do not hammer the volume.

_devices: "dict | None" = None
_last_persist = 0.0


def _ensure_loaded() -> dict:
    global _devices
    if _devices is None:
        if DEVICES_PATH.exists():
            try:
                _devices = json.loads(DEVICES_PATH.read_text())
            except Exception:
                _devices = {}
        else:
            _devices = {}
    return _devices


def record_checkin(panel_id: str, version: str, ip: str) -> None:
    global _last_persist
    devices = _ensure_loaded()
    changed = devices.get(panel_id, {}).get("version") != version
    devices[panel_id] = {"version": version, "ip": ip, "last_seen": int(time.time())}
    now = time.time()
    if changed or (now - _last_persist) > 30:
        try:
            _atomic_write_json(DEVICES_PATH, devices)
            _last_persist = now
        except Exception:
            pass  # check-in tracking is best-effort, never break the poll


def list_devices() -> dict:
    return dict(_ensure_loaded())
