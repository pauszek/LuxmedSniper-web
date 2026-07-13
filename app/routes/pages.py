"""All routes: HTML pages, HTMX partials and a JSON /status endpoint."""
import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import db, notify, security
from app.crypto import KeyStoreError
from app.engine import EngineError, SniperEngine
from app.luxmed_client import LuxmedError

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def _localdt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.astimezone(ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y %H:%M")


templates.env.filters["localdt"] = _localdt


def _engine(request: Request) -> SniperEngine:
    return request.app.state.engine


def _render(request: Request, template: str, **context) -> HTMLResponse:
    engine = _engine(request)
    context.setdefault("unlocked", engine.keystore.is_unlocked)
    context.setdefault("request", request)
    return templates.TemplateResponse(request, template, context)


# --------------------------------------------------------------------------
# first-run setup + GUI login
# --------------------------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if security.gui_password_is_set():
        return RedirectResponse("/", status_code=303)
    return _render(request, "setup.html")


@router.post("/setup")
def setup_submit(
    request: Request,
    gui_password: Annotated[str, Form()],
    gui_password2: Annotated[str, Form()],
    master_key: Annotated[str, Form()],
    master_key2: Annotated[str, Form()],
):
    if security.gui_password_is_set():
        return RedirectResponse("/", status_code=303)
    if gui_password != gui_password2 or master_key != master_key2:
        return _render(request, "setup.html", error="Hasła się nie zgadzają.")
    if len(gui_password) < 8 or len(master_key) < 12:
        return _render(
            request, "setup.html",
            error="Hasło GUI: min. 8 znaków, master key: min. 12 znaków.",
        )
    engine = _engine(request)
    security.set_gui_password(gui_password)
    if not engine.keystore.is_initialized:
        engine.keystore.initialize(master_key)
    response = RedirectResponse("/settings?msg=Skonfigurowano.+Podaj+teraz+dane+Luxmed+i+ntfy.", status_code=303)
    security.create_session(response)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not security.gui_password_is_set():
        return RedirectResponse("/setup", status_code=303)
    if security.has_valid_session(request):
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html")


@router.post("/login")
def login_submit(request: Request, password: Annotated[str, Form()]):
    if not security.check_gui_password(password):
        return _render(request, "login.html", error="Błędne hasło.")
    response = RedirectResponse("/", status_code=303)
    security.create_session(response)
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    security.destroy_session(request, response)
    return response


# --------------------------------------------------------------------------
# master key: unlock / lock
# --------------------------------------------------------------------------

@router.get("/unlock", response_class=HTMLResponse)
def unlock_page(request: Request):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    return _render(request, "unlock.html")


@router.post("/unlock")
def unlock_submit(request: Request, master_key: Annotated[str, Form()]):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    engine = _engine(request)
    try:
        engine.keystore.unlock(master_key)
    except KeyStoreError as e:
        return _render(request, "unlock.html", error=f"Odblokowanie nieudane: {e}")
    engine.invalidate_client()
    return RedirectResponse("/?msg=Odblokowano.", status_code=303)


@router.post("/lock")
def lock(request: Request):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    engine = _engine(request)
    engine.keystore.lock()
    engine.invalidate_client()
    return RedirectResponse("/unlock", status_code=303)


# --------------------------------------------------------------------------
# dashboard + monitor CRUD
# --------------------------------------------------------------------------

def _monitors_with_found() -> list[dict]:
    monitors = db.list_monitors()
    for monitor in monitors:
        monitor["latest_found"] = db.latest_found_for_monitor(monitor["id"])
    return monitors


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str | None = None):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    engine = _engine(request)
    return _render(
        request, "dashboard.html",
        monitors=_monitors_with_found(),
        status=engine.status(),
        msg=msg,
    )


@router.get("/partials/monitors", response_class=HTMLResponse)
def monitors_partial(request: Request):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    return _render(request, "partials/monitor_rows.html", monitors=_monitors_with_found())


def _monitor_form_context(engine: SniperEngine) -> dict:
    """Dropdown data; empty lists + error message when Luxmed is unreachable."""
    try:
        return {"cities": engine.get_cities(), "services": engine.get_services(), "dict_error": None}
    except (EngineError, KeyStoreError, LuxmedError, Exception) as e:
        return {"cities": [], "services": [], "dict_error": str(e)}


@router.get("/monitors/new", response_class=HTMLResponse)
def monitor_new_page(request: Request):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    return _render(request, "monitor_form.html", monitor=None, **_monitor_form_context(_engine(request)))


@router.get("/monitors/{monitor_id}/edit", response_class=HTMLResponse)
def monitor_edit_page(request: Request, monitor_id: int):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        return RedirectResponse("/?msg=Nie+ma+takiego+monitora.", status_code=303)
    return _render(request, "monitor_form.html", monitor=monitor, **_monitor_form_context(_engine(request)))


def _resolve_names(
    engine: SniperEngine,
    city_id: int,
    service_id: int,
    clinic_ids: list[int],
    doctor_ids: list[int],
) -> dict:
    city_name = next((str(c["name"]) for c in engine.get_cities() if c["id"] == city_id), str(city_id))
    service_name = next((s["name"] for s in engine.get_services() if s["id"] == service_id), str(service_id))
    clinic_names: list[str] = []
    doctor_names: list[str] = []
    if clinic_ids or doctor_ids:
        fd = engine.get_facilities_and_doctors(city_id, service_id)
        clinic_names = [f["name"] for f in fd["facilities"] if f["id"] in clinic_ids]
        doctor_names = [d["name"] for d in fd["doctors"] if d["id"] in doctor_ids]
    return {
        "city_name": city_name,
        "service_name": service_name,
        "clinic_names": clinic_names,
        "doctor_names": doctor_names,
    }


@router.post("/monitors")
def monitor_create(
    request: Request,
    name: Annotated[str, Form()],
    city_id: Annotated[int, Form()],
    service_id: Annotated[int, Form()],
    lookup_days: Annotated[int, Form()] = 14,
    clinic_ids: Annotated[list[int], Form()] = [],
    doctor_ids: Annotated[list[int], Form()] = [],
    enabled: Annotated[str | None, Form()] = None,
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    engine = _engine(request)
    try:
        names = _resolve_names(engine, city_id, service_id, clinic_ids, doctor_ids)
    except Exception:
        names = {"city_name": str(city_id), "service_name": str(service_id),
                 "clinic_names": [], "doctor_names": []}
    db.create_monitor(
        name=name.strip() or f"Monitor {city_id}/{service_id}",
        city_id=city_id,
        service_id=service_id,
        clinic_ids=clinic_ids,
        doctor_ids=doctor_ids,
        lookup_days=max(1, min(lookup_days, 60)),
        enabled=enabled is not None,
        **names,
    )
    engine.reschedule()
    return RedirectResponse("/?msg=Monitor+dodany.", status_code=303)


@router.post("/monitors/{monitor_id}")
def monitor_update(
    request: Request,
    monitor_id: int,
    name: Annotated[str, Form()],
    city_id: Annotated[int, Form()],
    service_id: Annotated[int, Form()],
    lookup_days: Annotated[int, Form()] = 14,
    clinic_ids: Annotated[list[int], Form()] = [],
    doctor_ids: Annotated[list[int], Form()] = [],
    enabled: Annotated[str | None, Form()] = None,
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    if db.get_monitor(monitor_id) is None:
        return RedirectResponse("/?msg=Nie+ma+takiego+monitora.", status_code=303)
    engine = _engine(request)
    try:
        names = _resolve_names(engine, city_id, service_id, clinic_ids, doctor_ids)
    except Exception:
        names = {"city_name": str(city_id), "service_name": str(service_id),
                 "clinic_names": [], "doctor_names": []}
    db.update_monitor(
        monitor_id,
        name=name.strip(),
        city_id=city_id,
        service_id=service_id,
        clinic_ids=clinic_ids,
        doctor_ids=doctor_ids,
        lookup_days=max(1, min(lookup_days, 60)),
        enabled=enabled is not None,
        **names,
    )
    engine.reschedule()
    return RedirectResponse("/?msg=Monitor+zapisany.", status_code=303)


@router.post("/monitors/{monitor_id}/toggle", response_class=HTMLResponse)
def monitor_toggle(request: Request, monitor_id: int):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        return Response(status_code=404)
    db.update_monitor(monitor_id, enabled=not monitor["enabled"])
    engine = _engine(request)
    engine.reschedule()
    return _render(request, "partials/monitor_rows.html", monitors=_monitors_with_found())


@router.post("/monitors/{monitor_id}/check", response_class=HTMLResponse)
def monitor_check_now(request: Request, monitor_id: int):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    engine = _engine(request)
    engine.run_now(monitor_id)
    db.set_monitor_check_result(monitor_id, "sprawdzanie…")
    return _render(request, "partials/monitor_rows.html", monitors=_monitors_with_found())


@router.post("/monitors/{monitor_id}/delete", response_class=HTMLResponse)
def monitor_delete(request: Request, monitor_id: int):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    db.delete_monitor(monitor_id)
    engine = _engine(request)
    engine.reschedule()
    return _render(request, "partials/monitor_rows.html", monitors=_monitors_with_found())


# --------------------------------------------------------------------------
# dictionaries (HTMX cascade for the monitor form)
# --------------------------------------------------------------------------

@router.get("/dictionaries/facilities-doctors", response_class=HTMLResponse)
def facilities_doctors_partial(
    request: Request,
    city_id: str = "",
    service_id: str = "",
    monitor_id: str = "",
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    # HTMX sends empty strings while the cascade is incomplete — parse leniently.
    city_id = int(city_id) if city_id.isdigit() else None
    service_id = int(service_id) if service_id.isdigit() else None
    monitor_id = int(monitor_id) if monitor_id.isdigit() else None
    if not city_id or not service_id:
        return _render(
            request, "partials/facilities_doctors.html",
            facilities=[], doctors=[], selected_clinics=[], selected_doctors=[],
            fd_error="Wybierz najpierw miasto i usługę.",
        )
    selected_clinics: list[int] = []
    selected_doctors: list[int] = []
    if monitor_id:
        monitor = db.get_monitor(monitor_id)
        if monitor and monitor["city_id"] == city_id and monitor["service_id"] == service_id:
            selected_clinics = monitor["clinic_ids"]
            selected_doctors = monitor["doctor_ids"]
    try:
        fd = _engine(request).get_facilities_and_doctors(city_id, service_id)
    except Exception as e:
        return _render(
            request, "partials/facilities_doctors.html",
            facilities=[], doctors=[], selected_clinics=[], selected_doctors=[],
            fd_error=f"Nie udało się pobrać listy: {e}",
        )
    return _render(
        request, "partials/facilities_doctors.html",
        facilities=fd["facilities"], doctors=fd["doctors"],
        selected_clinics=selected_clinics, selected_doctors=selected_doctors,
        fd_error=None,
    )


# --------------------------------------------------------------------------
# found appointments + status
# --------------------------------------------------------------------------

@router.get("/found", response_class=HTMLResponse)
def found_page(request: Request):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    return _render(request, "found.html", found=db.list_found(200))


@router.get("/status")
def status(request: Request):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    return JSONResponse(_engine(request).status())


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def _settings_context() -> dict:
    pushover = notify.pushover_settings()
    return {
        "luxmed_login": db.get_setting("luxmed_login", "") or "",
        "has_password": db.get_setting("luxmed_password_enc") is not None,
        "provider": notify.active_provider(),
        "pushover_user_key": pushover["user_key"],
        "has_pushover_token": bool(pushover["api_token"]),
        "ntfy": notify.ntfy_settings(),
        "interval": SniperEngine.interval_minutes(),
        "daily_limit": SniperEngine.daily_limit(),
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str | None = None):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    return _render(request, "settings.html", msg=msg, **_settings_context())


@router.post("/settings/credentials")
def settings_credentials(
    request: Request,
    luxmed_login: Annotated[str, Form()],
    luxmed_password: Annotated[str, Form()] = "",
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    engine = _engine(request)
    if not engine.keystore.is_unlocked:
        return _render(
            request, "settings.html", **_settings_context(),
            error="Aplikacja zablokowana — najpierw podaj master key (Odblokuj).",
        )
    db.set_setting("luxmed_login", luxmed_login.strip())
    if luxmed_password:
        db.set_setting("luxmed_password_enc", engine.keystore.encrypt(luxmed_password))
    engine.invalidate_client()
    return RedirectResponse("/settings?msg=Dane+Luxmed+zapisane+(has%C5%82o+zaszyfrowane).", status_code=303)


@router.post("/settings/provider")
def settings_provider(request: Request, provider: Annotated[str, Form()]):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    if provider not in notify.PROVIDERS:
        provider = "pushover"
    db.set_setting("notification_provider", provider)
    return RedirectResponse("/settings?msg=Aktywny+dostawca+powiadomie%C5%84+zmieniony.", status_code=303)


@router.post("/settings/pushover")
def settings_pushover(
    request: Request,
    pushover_user_key: Annotated[str, Form()],
    pushover_api_token: Annotated[str, Form()] = "",
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    db.set_setting("pushover_user_key", pushover_user_key.strip())
    if pushover_api_token.strip():
        db.set_setting("pushover_api_token", pushover_api_token.strip())
    return RedirectResponse("/settings?msg=Ustawienia+Pushover+zapisane.", status_code=303)


@router.post("/settings/ntfy")
def settings_ntfy(
    request: Request,
    ntfy_url: Annotated[str, Form()],
    ntfy_topic: Annotated[str, Form()],
    ntfy_token: Annotated[str, Form()] = "",
    ntfy_priority: Annotated[str, Form()] = "high",
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    db.set_setting("ntfy_url", ntfy_url.strip())
    db.set_setting("ntfy_topic", ntfy_topic.strip())
    db.set_setting("ntfy_token", ntfy_token.strip())
    db.set_setting("ntfy_priority", ntfy_priority)
    return RedirectResponse("/settings?msg=Ustawienia+ntfy+zapisane.", status_code=303)


@router.post("/settings/general")
def settings_general(
    request: Request,
    interval: Annotated[int, Form()],
    daily_limit: Annotated[int, Form()],
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    db.set_setting("check_interval_minutes", str(interval))
    db.set_setting("daily_request_limit", str(max(10, min(daily_limit, 2000))))
    engine = _engine(request)
    engine.reschedule()
    return RedirectResponse("/settings?msg=Zapisano+(dolna+granica+interwa%C5%82u+to+5+min).", status_code=303)


@router.post("/settings/gui-password")
def settings_gui_password(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    new_password2: Annotated[str, Form()],
):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    if not security.check_gui_password(current_password):
        return _render(request, "settings.html", **_settings_context(), error="Błędne obecne hasło.")
    if new_password != new_password2 or len(new_password) < 8:
        return _render(
            request, "settings.html", **_settings_context(),
            error="Nowe hasła muszą być identyczne i mieć min. 8 znaków.",
        )
    security.set_gui_password(new_password)
    return RedirectResponse("/settings?msg=Has%C5%82o+GUI+zmienione.", status_code=303)


@router.post("/test-notification", response_class=HTMLResponse)
def test_notification(request: Request):
    if (redirect := security.auth_redirect(request)) is not None:
        return redirect
    try:
        notify.send_push(
            title="Test LuxmedSniper",
            message="Działa! 🎯 Tak będą wyglądać powiadomienia o wizytach.",
            priority="default",
            tags=["white_check_mark"],
        )
    except Exception as e:
        return HTMLResponse(f'<span class="flash error">Błąd: {e}</span>')
    return HTMLResponse('<span class="flash ok">Wysłano — sprawdź telefon.</span>')
