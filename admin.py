"""
Admin web UI: first-run password setup, login, and the config form.

Everything under /admin is gated. On first run (no password set) the user is
sent to /admin/setup to create one. /api/status and /healthz stay public so
the CYD, which cannot log in, can still poll.
"""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import firmware
import probe

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SEE_OTHER = 303
FOUND = 302


def _logged_in(request: Request) -> bool:
    return bool(request.session.get("auth"))


def _num(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


@router.get("", response_class=HTMLResponse)
def home(request: Request):
    cfg = config.get()
    if not cfg.admin_password_hash:
        return RedirectResponse("/admin/setup", status_code=FOUND)
    if not _logged_in(request):
        return RedirectResponse("/admin/login", status_code=FOUND)
    return templates.TemplateResponse(
        request,
        "config.html",
        {"cfg": cfg, "saved": request.query_params.get("saved")},
    )


# --- first-run setup --------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    if config.get().admin_password_hash:
        return RedirectResponse("/admin/login", status_code=FOUND)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup")
async def setup_submit(request: Request):
    cfg = config.get()
    if cfg.admin_password_hash:
        return RedirectResponse("/admin/login", status_code=SEE_OTHER)
    if config.load_error:
        # The config file exists but could not be read (perms/corruption). The
        # password hash likely lives in it; letting setup run would overwrite
        # the real config. Fail loudly instead.
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": f"Cannot read the saved config ({config.load_error}). "
                      "Fix the /data volume (app runs as uid 1000), then restart."},
            status_code=500,
        )
    form = await request.form()
    password = form.get("password", "")
    confirm = form.get("confirm", "")
    error = None
    if len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm:
        error = "Passwords do not match."
    if error:
        return templates.TemplateResponse(
            request, "setup.html", {"error": error}, status_code=400
        )
    cfg.admin_password_hash = config.hash_password(password)
    config.save(cfg)
    request.session["auth"] = True
    return RedirectResponse("/admin", status_code=SEE_OTHER)


# --- login / logout ---------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if not config.get().admin_password_hash:
        return RedirectResponse("/admin/setup", status_code=FOUND)
    return templates.TemplateResponse(request, "login.html", {"error": None})


# One login verification at a time: PBKDF2 is deliberately expensive, so
# without this an unauthenticated client could fire parallel POSTs and burn a
# core per request. Serializing also makes the 1s failure delay a real global
# rate limit instead of a per-request one.
_login_lock = asyncio.Lock()


@router.post("/login")
async def login_submit(request: Request):
    cfg = config.get()
    if not cfg.admin_password_hash:
        # no password set yet: same redirect the GET route does, instead of a
        # dead-end "Incorrect password"
        return RedirectResponse("/admin/setup", status_code=SEE_OTHER)
    form = await request.form()
    async with _login_lock:
        ok = config.verify_password(form.get("password", ""), cfg.admin_password_hash)
        if not ok:
            await asyncio.sleep(1.0)  # blunt brute-force attempts
    if ok:
        request.session["auth"] = True
        return RedirectResponse("/admin", status_code=SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Incorrect password."},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=SEE_OTHER)


# --- save config ------------------------------------------------------------

@router.post("/config")
async def save_config(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/admin/login", status_code=SEE_OTHER)
    form = await request.form()
    cfg = config.get()
    cfg.mock = form.get("mock") is not None
    cfg.pve_host = form.get("pve_host", "").strip()
    cfg.pve_token_id = form.get("pve_token_id", "").strip()
    cfg.truenas_host = form.get("truenas_host", "").strip()
    cfg.pbs_host = form.get("pbs_host", "").strip()
    cfg.pbs_token_id = form.get("pbs_token_id", "").strip()
    cfg.pbs_node = form.get("pbs_node", "").strip() or "localhost"
    cfg.unifi_host = form.get("unifi_host", "").strip()
    cfg.unifi_site = form.get("unifi_site", "").strip() or "default"
    # Secrets are never rendered back into the form; an empty submit means
    # "keep the saved value" so viewing/saving the page cannot leak or wipe them.
    for field in ("pve_secret", "truenas_key", "pbs_secret", "unifi_key"):
        value = (form.get(field) or "").strip()
        if value:
            setattr(cfg, field, value)
    cfg.mem_warn = _num(form.get("mem_warn"), cfg.mem_warn)
    cfg.pool_warn = _num(form.get("pool_warn"), cfg.pool_warn)
    cfg.pbs_warn = _num(form.get("pbs_warn"), cfg.pbs_warn)
    cfg.cache_ttl = _num(form.get("cache_ttl"), cfg.cache_ttl)
    cfg.http_timeout = _num(form.get("http_timeout"), cfg.http_timeout)
    config.save(cfg)

    import app  # lazy, avoids a circular import at module load
    app.invalidate_cache()
    return RedirectResponse("/admin?saved=1", status_code=SEE_OTHER)


# --- connection tests (probe the just-typed values; a blank secret falls
# back to the saved one, since secrets are never echoed into the form) -------

def _form_val(f, key: str, saved: str = "") -> str:
    return (f.get(key) or "").strip() or saved


def _saved_secret(f, host_key: str, saved_host: str, saved_secret: str) -> str:
    """A blank secret means "test with the saved one", but only against the
    saved host: otherwise a Test could be pointed at an arbitrary host and
    used to exfiltrate a stored credential in the auth header."""
    return saved_secret if _form_val(f, host_key) == (saved_host or "").strip() else ""


@router.post("/test/pve")
async def test_pve(request: Request):
    if not _logged_in(request):
        return JSONResponse({"ok": False, "detail": "Not authenticated."}, status_code=401)
    f = await request.form()
    cfg = config.get()
    result = await probe.probe_pve(
        _form_val(f, "pve_host"), _form_val(f, "pve_token_id"),
        _form_val(f, "pve_secret", _saved_secret(f, "pve_host", cfg.pve_host, cfg.pve_secret)),
        cfg.http_timeout,
    )
    return JSONResponse(result)


@router.post("/test/truenas")
async def test_truenas(request: Request):
    if not _logged_in(request):
        return JSONResponse({"ok": False, "detail": "Not authenticated."}, status_code=401)
    f = await request.form()
    cfg = config.get()
    result = await probe.probe_truenas(
        _form_val(f, "truenas_host"),
        _form_val(f, "truenas_key", _saved_secret(f, "truenas_host", cfg.truenas_host, cfg.truenas_key)),
        cfg.http_timeout,
    )
    return JSONResponse(result)


@router.post("/test/pbs")
async def test_pbs(request: Request):
    if not _logged_in(request):
        return JSONResponse({"ok": False, "detail": "Not authenticated."}, status_code=401)
    f = await request.form()
    cfg = config.get()
    result = await probe.probe_pbs(
        _form_val(f, "pbs_host"), _form_val(f, "pbs_token_id"),
        _form_val(f, "pbs_secret", _saved_secret(f, "pbs_host", cfg.pbs_host, cfg.pbs_secret)),
        cfg.http_timeout,
    )
    return JSONResponse(result)


@router.post("/test/unifi")
async def test_unifi(request: Request):
    if not _logged_in(request):
        return JSONResponse({"ok": False, "detail": "Not authenticated."}, status_code=401)
    f = await request.form()
    cfg = config.get()
    result = await probe.probe_unifi(
        _form_val(f, "unifi_host"),
        _form_val(f, "unifi_key", _saved_secret(f, "unifi_host", cfg.unifi_host, cfg.unifi_key)),
        _form_val(f, "unifi_site", "default"), cfg.http_timeout,
    )
    return JSONResponse(result)


# --- firmware / OTA ---------------------------------------------------------

def _firmware_ctx(request: Request, uploaded=None, error=None) -> dict:
    return {
        "meta": firmware.get_meta(),
        "devices": firmware.list_devices(),
        "manifest_url": str(request.base_url) + "firmware/manifest.json",
        "uploaded": uploaded,
        "error": error,
    }


@router.get("/firmware", response_class=HTMLResponse)
def firmware_page(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/admin/login", status_code=FOUND)
    return templates.TemplateResponse(
        request, "firmware.html",
        _firmware_ctx(request, uploaded=request.query_params.get("uploaded")),
    )


# Generous cap for an ESP32 OTA image (real builds are ~1.5-2 MB on a 4 MB
# flash part). Rejecting oversized posts up front keeps a bad upload from
# ballooning the RAM-backed /tmp spool in the read-only container.
MAX_FW_BYTES = 8 * 1024 * 1024


@router.post("/firmware")
async def firmware_upload(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/admin/login", status_code=SEE_OTHER)
    try:
        if int(request.headers.get("content-length", 0)) > MAX_FW_BYTES + 65536:
            return templates.TemplateResponse(
                request, "firmware.html",
                _firmware_ctx(request, error="Upload too large for an ESP32 OTA image (8 MB max)."),
                status_code=413,
            )
    except ValueError:
        pass
    form = await request.form()
    version = (form.get("version") or "").strip()
    summary = (form.get("summary") or "").strip()
    upload = form.get("file")

    error = None
    data = b""
    if not version:
        error = "Version is required."
    elif upload is None or not getattr(upload, "filename", ""):
        error = "Choose a firmware file to upload."
    else:
        data = await upload.read()
        if len(data) > MAX_FW_BYTES:
            error = "Upload too large for an ESP32 OTA image (8 MB max)."
        elif not data or data[0] != 0xE9:
            error = "That does not look like an ESP32 firmware image (missing 0xE9 magic byte)."

    if error:
        return templates.TemplateResponse(
            request, "firmware.html",
            _firmware_ctx(request, error=error),
            status_code=400,
        )

    firmware.save_firmware(data, version, summary)
    return RedirectResponse("/admin/firmware?uploaded=1", status_code=SEE_OTHER)
