"""
Admin web UI: first-run password setup, login, and the config form.

Everything under /admin is gated. On first run (no password set) the user is
sent to /admin/setup to create one. /api/status and /healthz stay public so
the CYD, which cannot log in, can still poll.
"""

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


@router.post("/login")
async def login_submit(request: Request):
    cfg = config.get()
    form = await request.form()
    if config.verify_password(form.get("password", ""), cfg.admin_password_hash):
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
    cfg.pve_secret = form.get("pve_secret", "").strip()
    cfg.truenas_host = form.get("truenas_host", "").strip()
    cfg.truenas_key = form.get("truenas_key", "").strip()
    cfg.pbs_host = form.get("pbs_host", "").strip()
    cfg.pbs_token_id = form.get("pbs_token_id", "").strip()
    cfg.pbs_secret = form.get("pbs_secret", "").strip()
    cfg.pbs_node = form.get("pbs_node", "").strip() or "localhost"
    cfg.mem_warn = _num(form.get("mem_warn"), cfg.mem_warn)
    cfg.pool_warn = _num(form.get("pool_warn"), cfg.pool_warn)
    cfg.pbs_warn = _num(form.get("pbs_warn"), cfg.pbs_warn)
    cfg.cache_ttl = _num(form.get("cache_ttl"), cfg.cache_ttl)
    cfg.http_timeout = _num(form.get("http_timeout"), cfg.http_timeout)
    config.save(cfg)

    import app  # lazy, avoids a circular import at module load
    app.invalidate_cache()
    return RedirectResponse("/admin?saved=1", status_code=SEE_OTHER)


# --- connection tests (probe the just-typed values, not saved creds) --------

@router.post("/test/pve")
async def test_pve(request: Request):
    if not _logged_in(request):
        return JSONResponse({"ok": False, "detail": "Not authenticated."}, status_code=401)
    f = await request.form()
    result = await probe.probe_pve(
        (f.get("pve_host") or "").strip(),
        (f.get("pve_token_id") or "").strip(),
        (f.get("pve_secret") or "").strip(),
        config.get().http_timeout,
    )
    return JSONResponse(result)


@router.post("/test/truenas")
async def test_truenas(request: Request):
    if not _logged_in(request):
        return JSONResponse({"ok": False, "detail": "Not authenticated."}, status_code=401)
    f = await request.form()
    result = await probe.probe_truenas(
        (f.get("truenas_host") or "").strip(),
        (f.get("truenas_key") or "").strip(),
        config.get().http_timeout,
    )
    return JSONResponse(result)


@router.post("/test/pbs")
async def test_pbs(request: Request):
    if not _logged_in(request):
        return JSONResponse({"ok": False, "detail": "Not authenticated."}, status_code=401)
    f = await request.form()
    result = await probe.probe_pbs(
        (f.get("pbs_host") or "").strip(),
        (f.get("pbs_token_id") or "").strip(),
        (f.get("pbs_secret") or "").strip(),
        config.get().http_timeout,
    )
    return JSONResponse(result)


# --- firmware / OTA ---------------------------------------------------------

@router.get("/firmware", response_class=HTMLResponse)
def firmware_page(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/admin/login", status_code=FOUND)
    return templates.TemplateResponse(
        request,
        "firmware.html",
        {
            "meta": firmware.get_meta(),
            "devices": firmware.list_devices(),
            "manifest_url": str(request.base_url) + "firmware/manifest.json",
            "uploaded": request.query_params.get("uploaded"),
            "error": None,
        },
    )


@router.post("/firmware")
async def firmware_upload(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/admin/login", status_code=SEE_OTHER)
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
        if not data or data[0] != 0xE9:
            error = "That does not look like an ESP32 firmware image (missing 0xE9 magic byte)."

    if error:
        return templates.TemplateResponse(
            request,
            "firmware.html",
            {
                "meta": firmware.get_meta(),
                "devices": firmware.list_devices(),
                "manifest_url": str(request.base_url) + "firmware/manifest.json",
                "uploaded": None,
                "error": error,
            },
            status_code=400,
        )

    firmware.save_firmware(data, version, summary)
    return RedirectResponse("/admin/firmware?uploaded=1", status_code=SEE_OTHER)
