"""SQLite persistence: monitors, found appointments, settings, request log.

Connection-per-operation keeps this safe across FastAPI and APScheduler
threads without a shared-connection lock.
"""
import datetime
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    city_id       INTEGER NOT NULL,
    city_name     TEXT    NOT NULL DEFAULT '',
    service_id    INTEGER NOT NULL,
    service_name  TEXT    NOT NULL DEFAULT '',
    clinic_ids    TEXT    NOT NULL DEFAULT '[]',
    doctor_ids    TEXT    NOT NULL DEFAULT '[]',
    clinic_names  TEXT    NOT NULL DEFAULT '[]',
    doctor_names  TEXT    NOT NULL DEFAULT '[]',
    lookup_days   INTEGER NOT NULL DEFAULT 14,
    created_at    TEXT    NOT NULL,
    last_check_at TEXT,
    last_status   TEXT,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS found_appointments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id       INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    doctor           TEXT    NOT NULL,
    clinic           TEXT    NOT NULL,
    appointment_date TEXT    NOT NULL,
    found_at         TEXT    NOT NULL,
    notified         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (monitor_id, doctor, appointment_date)
);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.datetime.now(tz=datetime.UTC).isoformat()


@contextmanager
def connect(db_path: Path | None = None):
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- settings (key/value) ---

def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def delete_setting(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --- monitors ---

def _monitor_from_row(row: sqlite3.Row) -> dict:
    monitor = dict(row)
    for field in ("clinic_ids", "doctor_ids", "clinic_names", "doctor_names"):
        monitor[field] = json.loads(monitor[field])
    monitor["enabled"] = bool(monitor["enabled"])
    return monitor


def list_monitors() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM monitors ORDER BY id").fetchall()
    return [_monitor_from_row(r) for r in rows]


def get_monitor(monitor_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
    return _monitor_from_row(row) if row else None


def create_monitor(
    name: str,
    city_id: int,
    city_name: str,
    service_id: int,
    service_name: str,
    clinic_ids: list[int],
    doctor_ids: list[int],
    clinic_names: list[str],
    doctor_names: list[str],
    lookup_days: int,
    enabled: bool = True,
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO monitors (name, enabled, city_id, city_name, service_id, service_name,"
            " clinic_ids, doctor_ids, clinic_names, doctor_names, lookup_days, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name, int(enabled), city_id, city_name, service_id, service_name,
                json.dumps(clinic_ids), json.dumps(doctor_ids),
                json.dumps(clinic_names), json.dumps(doctor_names),
                lookup_days, _utcnow(),
            ),
        )
        return cursor.lastrowid


def update_monitor(monitor_id: int, **fields) -> None:
    if not fields:
        return
    for key in ("clinic_ids", "doctor_ids", "clinic_names", "doctor_names"):
        if key in fields:
            fields[key] = json.dumps(fields[key])
    if "enabled" in fields:
        fields["enabled"] = int(fields["enabled"])
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE monitors SET {assignments} WHERE id = ?",
            (*fields.values(), monitor_id),
        )


def delete_monitor(monitor_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))


def set_monitor_check_result(monitor_id: int, status: str, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE monitors SET last_check_at = ?, last_status = ?, last_error = ? WHERE id = ?",
            (_utcnow(), status, error, monitor_id),
        )


# --- found appointments (also the notification dedup store) ---

def record_appointment(monitor_id: int, doctor: str, clinic: str, appointment_date: str) -> bool:
    """Returns True when this appointment is new (i.e. should be notified)."""
    with connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO found_appointments"
            " (monitor_id, doctor, clinic, appointment_date, found_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (monitor_id, doctor, clinic, appointment_date, _utcnow()),
        )
        return cursor.rowcount > 0


def mark_notified(monitor_id: int, doctor: str, appointment_date: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE found_appointments SET notified = 1"
            " WHERE monitor_id = ? AND doctor = ? AND appointment_date = ?",
            (monitor_id, doctor, appointment_date),
        )


def list_found(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.*, m.name AS monitor_name FROM found_appointments f"
            " LEFT JOIN monitors m ON m.id = f.monitor_id"
            " ORDER BY f.found_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_found_for_monitor(monitor_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM found_appointments WHERE monitor_id = ?"
            " ORDER BY found_at DESC LIMIT 1",
            (monitor_id,),
        ).fetchone()
    return dict(row) if row else None


# --- request accounting (daily fair-use budget) ---

def record_api_call() -> None:
    with connect() as conn:
        conn.execute("INSERT INTO api_calls (at) VALUES (?)", (_utcnow(),))
        # keep the table from growing forever
        cutoff = (datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=7)).isoformat()
        conn.execute("DELETE FROM api_calls WHERE at < ?", (cutoff,))


def api_calls_last_24h() -> int:
    cutoff = (datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=24)).isoformat()
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM api_calls WHERE at >= ?", (cutoff,)).fetchone()
    return row["n"]
