"""Push notifications with a pluggable provider (Pushover or ntfy).

The active provider is chosen by the `notification_provider` setting. Pushover
is the default: one-time paid, dead simple, works everywhere, and was already
supported by the upstream CLI. ntfy (self-hosted + APNs relay) stays available
for anyone who prefers to keep message content on their own infrastructure.
"""
import requests
from loguru import logger

from app import db

PROVIDERS = ("pushover", "ntfy")


class NotifyError(Exception):
    pass


def active_provider() -> str:
    provider = db.get_setting("notification_provider", "pushover") or "pushover"
    return provider if provider in PROVIDERS else "pushover"


# --------------------------------------------------------------------------
# Pushover  (https://pushover.net/api)
# --------------------------------------------------------------------------

# Pushover priority is -2..2; 2 (emergency) needs retry/expire params, so we
# cap at 1 (high) — bypasses quiet hours and alerts prominently, no retries.
_PUSHOVER_PRIORITY = {"min": -2, "low": -1, "default": 0, "high": 1, "urgent": 1}


def pushover_settings() -> dict:
    return {
        "user_key": db.get_setting("pushover_user_key", "") or "",
        "api_token": db.get_setting("pushover_api_token", "") or "",
    }


def _send_pushover(title: str, message: str, priority: str) -> None:
    settings = pushover_settings()
    if not settings["user_key"] or not settings["api_token"]:
        raise NotifyError("Pushover nie skonfigurowany (Ustawienia → Pushover: user key + API token)")
    response = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={
            "token": settings["api_token"],
            "user": settings["user_key"],
            "title": title,
            "message": message,
            "priority": _PUSHOVER_PRIORITY.get(priority, 0),
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise NotifyError(f"Pushover error HTTP {response.status_code}: {response.text[:200]}")
    logger.info("Pushover push sent: {}", title)


# --------------------------------------------------------------------------
# ntfy  (self-hosted + upstream-base-url relay for iOS)
# --------------------------------------------------------------------------

_NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def ntfy_settings() -> dict:
    return {
        "url": (db.get_setting("ntfy_url", "https://ntfy.sh") or "").rstrip("/"),
        "topic": db.get_setting("ntfy_topic", "") or "",
        "token": db.get_setting("ntfy_token", "") or "",
        "priority": db.get_setting("ntfy_priority", "high") or "high",
    }


def _send_ntfy(title: str, message: str, priority: str, tags: list[str] | None) -> None:
    settings = ntfy_settings()
    if not settings["topic"]:
        raise NotifyError("ntfy nie skonfigurowany (Ustawienia → ntfy: topic)")
    headers = {}
    if settings["token"]:
        headers["Authorization"] = f"Bearer {settings['token']}"
    payload = {
        "topic": settings["topic"],
        "title": title,
        "message": message,
        "priority": _NTFY_PRIORITY.get(priority, 4),
        "tags": tags if tags is not None else ["hospital", "pill", "syringe"],
    }
    response = requests.post(settings["url"], json=payload, headers=headers, timeout=10)
    if response.status_code != 200:
        raise NotifyError(f"ntfy error HTTP {response.status_code}: {response.text[:200]}")
    logger.info("ntfy push sent: {}", title)


# --------------------------------------------------------------------------
# provider-agnostic entry points
# --------------------------------------------------------------------------

def send_push(
    title: str,
    message: str,
    priority: str | None = None,
    tags: list[str] | None = None,
) -> None:
    provider = active_provider()
    if provider == "pushover":
        _send_pushover(title, message, priority or "high")
    else:
        _send_ntfy(title, message, priority or "high", tags)


def notify_appointment(monitor_name: str, doctor: str, clinic: str, date_local: str) -> None:
    send_push(
        title=f"Nowa wizyta: {monitor_name}",
        message=f"{date_local}\n{doctor}\n{clinic}",
        priority="urgent",
    )


def notify_warning(message: str) -> None:
    try:
        send_push(title="LuxmedSniper — uwaga", message=message, priority="default", tags=["warning"])
    except (NotifyError, requests.RequestException) as e:
        logger.warning("Could not send warning push: {}", e)
