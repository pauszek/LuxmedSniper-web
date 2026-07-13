"""Single-password GUI auth with in-memory session tokens.

This is a LAN app for one household — no users/roles. Sessions live in RAM,
so a restart logs everyone out (they'd need to re-unlock anyway).
"""
import datetime
import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from app import config, db
from app.crypto import hash_password, verify_password

_sessions: dict[str, datetime.datetime] = {}


def gui_password_is_set() -> bool:
    return db.get_setting("gui_password_hash") is not None


def set_gui_password(password: str) -> None:
    db.set_setting("gui_password_hash", hash_password(password))


def check_gui_password(password: str) -> bool:
    stored = db.get_setting("gui_password_hash")
    return stored is not None and verify_password(password, stored)


def create_session(response: Response) -> None:
    token = secrets.token_urlsafe(32)
    _sessions[token] = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
        days=config.SESSION_TTL_DAYS,
    )
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=config.SESSION_TTL_DAYS * 24 * 3600,
    )


def destroy_session(request: Request, response: Response) -> None:
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(config.SESSION_COOKIE)


def has_valid_session(request: Request) -> bool:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return False
    expires = _sessions.get(token)
    if expires is None:
        return False
    now = datetime.datetime.now(tz=datetime.UTC)
    if expires < now:
        _sessions.pop(token, None)
        return False
    # sliding expiry
    _sessions[token] = now + datetime.timedelta(days=config.SESSION_TTL_DAYS)
    return True


def auth_redirect(request: Request) -> RedirectResponse | None:
    """Where an unauthenticated/unconfigured request should land, or None if OK."""
    if not gui_password_is_set():
        return _redirect(request, "/setup")
    if not has_valid_session(request):
        return _redirect(request, "/login")
    return None


def _redirect(request: Request, target: str) -> RedirectResponse:
    # HTMX partial requests need HX-Redirect to escape the swap target.
    if request.headers.get("HX-Request"):
        response = Response(status_code=204)
        response.headers["HX-Redirect"] = target
        return response
    return RedirectResponse(target, status_code=303)
