"""Scheduler engine: one APScheduler job per monitor + fair-use protection.

Luxmed blocks accounts that poll too aggressively (1st offence: one day,
2nd offence: permanently), so every outgoing request passes a daily budget
guard, jobs get random jitter, the interval has a hard floor, and repeated
login failures auto-pause all polling for a configurable number of hours.
"""
import datetime
import threading
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app import config, db, notify
from app.crypto import KeyStore
from app.luxmed_client import LuxmedAuthError, LuxmedClient, LuxmedError


class EngineError(Exception):
    pass


class BudgetExceededError(EngineError):
    pass


class SniperEngine:
    def __init__(self, keystore: KeyStore):
        self.keystore = keystore
        self.scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        )
        self._client: LuxmedClient | None = None
        self._client_creds_fingerprint: str | None = None
        self._client_lock = threading.Lock()
        self._login_failures = 0
        self._dict_cache: dict[str, tuple[datetime.datetime, object]] = {}

    # --- lifecycle ---

    def start(self) -> None:
        self.scheduler.start()
        self.reschedule()

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    def reschedule(self) -> None:
        """Rebuild all jobs from the monitors table (call after any CRUD)."""
        for job in self.scheduler.get_jobs():
            job.remove()
        interval = self.interval_minutes()
        stagger = 0
        for monitor in db.list_monitors():
            if not monitor["enabled"]:
                continue
            # First run staggered a bit so monitors don't all fire at once
            # (they still share one login session when close together).
            first_run = (
                datetime.datetime.now(tz=datetime.UTC)
                + datetime.timedelta(seconds=15 + stagger)
            )
            stagger += 20
            self.scheduler.add_job(
                self.check_monitor,
                trigger=IntervalTrigger(
                    minutes=interval,
                    jitter=config.JITTER_SECONDS,
                    start_date=first_run,
                ),
                args=[monitor["id"]],
                id=f"monitor-{monitor['id']}",
                name=monitor["name"],
                replace_existing=True,
            )
        logger.info("Scheduled {} monitor job(s), interval {} min", stagger // 20, interval)

    def run_now(self, monitor_id: int) -> None:
        """One-shot manual check, run in the scheduler's thread pool."""
        self.scheduler.add_job(
            self.check_monitor,
            args=[monitor_id],
            id=f"manual-{monitor_id}-{datetime.datetime.now(tz=datetime.UTC).timestamp()}",
        )

    # --- settings-derived knobs ---

    @staticmethod
    def interval_minutes() -> int:
        raw = db.get_setting("check_interval_minutes", str(config.DEFAULT_INTERVAL_MINUTES))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = config.DEFAULT_INTERVAL_MINUTES
        return max(value, config.HARD_MIN_INTERVAL_MINUTES)

    @staticmethod
    def daily_limit() -> int:
        raw = db.get_setting("daily_request_limit", str(config.DEFAULT_DAILY_REQUEST_LIMIT))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return config.DEFAULT_DAILY_REQUEST_LIMIT

    @staticmethod
    def paused_until() -> datetime.datetime | None:
        raw = db.get_setting("paused_until")
        if not raw:
            return None
        until = datetime.datetime.fromisoformat(raw)
        if until <= datetime.datetime.now(tz=datetime.UTC):
            db.delete_setting("paused_until")
            return None
        return until

    # --- Luxmed client management ---

    def _budget_guard(self) -> None:
        if db.api_calls_last_24h() >= self.daily_limit():
            raise BudgetExceededError(
                f"Daily request budget ({self.daily_limit()}) exhausted — skipping until it frees up",
            )
        db.record_api_call()

    def credentials(self) -> tuple[str, str]:
        login = db.get_setting("luxmed_login")
        password_enc = db.get_setting("luxmed_password_enc")
        if not login or not password_enc:
            raise EngineError("Luxmed credentials are not configured")
        return login, self.keystore.decrypt(password_enc)

    def _get_client(self) -> LuxmedClient:
        login, password = self.credentials()
        fingerprint = f"{login}:{hash(password)}"
        with self._client_lock:
            if self._client is None or self._client_creds_fingerprint != fingerprint:
                self._client = LuxmedClient(login, password, on_request=self._budget_guard)
                self._client_creds_fingerprint = fingerprint
            client = self._client
        try:
            client.ensure_logged_in(config.LUXMED_SESSION_MAX_AGE_MINUTES)
        except LuxmedAuthError:
            self._register_login_failure()
            raise
        self._login_failures = 0
        return client

    def invalidate_client(self) -> None:
        """Drop the cached session (e.g. after credentials change)."""
        with self._client_lock:
            self._client = None
            self._client_creds_fingerprint = None

    def _register_login_failure(self) -> None:
        self._login_failures += 1
        logger.warning("Luxmed login failure #{}", self._login_failures)
        if self._login_failures >= config.LOGIN_FAILURES_BEFORE_PAUSE:
            until = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
                hours=config.LOGIN_FAILURE_PAUSE_HOURS,
            )
            db.set_setting("paused_until", until.isoformat())
            self._login_failures = 0
            logger.error("Auto-paused polling until {} after repeated login failures", until)
            notify.notify_warning(
                f"Logowanie do Luxmedu nie powiodło się {config.LOGIN_FAILURES_BEFORE_PAUSE}x z rzędu."
                f" Monitorowanie wstrzymane na {config.LOGIN_FAILURE_PAUSE_HOURS} h"
                " (ochrona przed blokadą konta). Sprawdź hasło w Ustawieniach.",
            )

    # --- the job ---

    def check_monitor(self, monitor_id: int) -> None:
        monitor = db.get_monitor(monitor_id)
        if monitor is None or not monitor["enabled"]:
            return
        if not self.keystore.is_unlocked:
            db.set_monitor_check_result(monitor_id, "locked", "Aplikacja zablokowana — podaj master key")
            return
        paused = self.paused_until()
        if paused is not None:
            db.set_monitor_check_result(
                monitor_id, "paused", f"Auto-pauza do {paused:%Y-%m-%d %H:%M} UTC",
            )
            return

        try:
            client = self._get_client()
            try:
                appointments = client.search_appointments(
                    city_id=monitor["city_id"],
                    service_id=monitor["service_id"],
                    clinic_ids=monitor["clinic_ids"],
                    doctor_ids=monitor["doctor_ids"],
                    lookup_days=monitor["lookup_days"],
                )
            except LuxmedAuthError:
                # Session died mid-flight — one forced re-login, one retry.
                self.invalidate_client()
                client = self._get_client()
                appointments = client.search_appointments(
                    city_id=monitor["city_id"],
                    service_id=monitor["service_id"],
                    clinic_ids=monitor["clinic_ids"],
                    doctor_ids=monitor["doctor_ids"],
                    lookup_days=monitor["lookup_days"],
                )
        except BudgetExceededError as e:
            logger.warning(str(e))
            db.set_monitor_check_result(monitor_id, "limit", str(e))
            return
        except (EngineError, LuxmedError, Exception) as e:
            logger.exception("Check failed for monitor {}: {}", monitor["name"], e)
            db.set_monitor_check_result(monitor_id, "error", str(e)[:500])
            return

        new_count = 0
        for appointment in appointments:
            date_iso = appointment.date.isoformat()
            if db.record_appointment(monitor_id, appointment.doctor, appointment.clinic, date_iso):
                new_count += 1
                date_local = appointment.date.astimezone(ZoneInfo("Europe/Warsaw"))
                logger.info(
                    "New appointment for {}: {} — {} at {}",
                    monitor["name"], date_local, appointment.doctor, appointment.clinic,
                )
                try:
                    notify.notify_appointment(
                        monitor_name=monitor["name"],
                        doctor=appointment.doctor,
                        clinic=appointment.clinic,
                        date_local=f"{date_local:%d.%m.%Y %H:%M}",
                    )
                    db.mark_notified(monitor_id, appointment.doctor, date_iso)
                except Exception as e:
                    logger.exception("Notification failed: {}", e)

        status = f"ok ({len(appointments)} terminów, {new_count} nowych)" if appointments else "ok (brak terminów)"
        db.set_monitor_check_result(monitor_id, status)

    # --- dictionaries (cached so the GUI doesn't hammer Luxmed) ---

    def _cached(self, key: str, loader):
        now = datetime.datetime.now(tz=datetime.UTC)
        hit = self._dict_cache.get(key)
        if hit is not None:
            at, value = hit
            if now - at < datetime.timedelta(hours=config.DICTIONARY_CACHE_TTL_HOURS):
                return value
        value = loader()
        self._dict_cache[key] = (now, value)
        return value

    def get_cities(self) -> list[dict]:
        return self._cached("cities", lambda: self._get_client().get_cities())

    def get_services(self) -> list[dict]:
        return self._cached("services", lambda: self._get_client().get_services())

    def get_facilities_and_doctors(self, city_id: int, service_id: int) -> dict:
        return self._cached(
            f"fd:{city_id}:{service_id}",
            lambda: self._get_client().get_facilities_and_doctors(city_id, service_id),
        )

    # --- GUI status snapshot ---

    def status(self) -> dict:
        jobs = [
            {
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in self.scheduler.get_jobs()
        ]
        paused = self.paused_until()
        return {
            "unlocked": self.keystore.is_unlocked,
            "scheduler_running": self.scheduler.running,
            "interval_minutes": self.interval_minutes(),
            "requests_last_24h": db.api_calls_last_24h(),
            "daily_limit": self.daily_limit(),
            "paused_until": paused.isoformat() if paused else None,
            "jobs": jobs,
        }
