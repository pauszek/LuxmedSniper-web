"""Application-level configuration (paths, network, safety limits).

Everything here is either an env var or a hard safety constant. Secrets are
never configured here — they live encrypted in SQLite (see crypto.py).
"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("SNIPER_DATA_DIR", "./data")).expanduser()
DB_PATH = DATA_DIR / "sniper.db"

HOST = os.environ.get("SNIPER_HOST", "0.0.0.0")
PORT = int(os.environ.get("SNIPER_PORT", "8080"))

# Optional auto-unlock: path to a file containing the master key (e.g. a
# Proxmox host directory bind-mounted into the LXC). If unset or missing,
# the app starts locked and waits for the key via the GUI.
MASTER_KEY_FILE = os.environ.get("SNIPER_MASTER_KEY_FILE", "")

# --- fair-use protection (Luxmed bans accounts for aggressive polling:
# first offence = 1 day lock, second = permanent) ---
# The exact threshold is not published; 30 min is the upstream project's
# conservative default. The floor below is the safety limit; the *real* rate
# cap in practice is DEFAULT_DAILY_REQUEST_LIMIT (rolling 24h budget), because
# once it's hit, checks skip until old calls age out of the window.
HARD_MIN_INTERVAL_MINUTES = 5    # floor; GUI cannot set a lower interval
DEFAULT_INTERVAL_MINUTES = 30
JITTER_SECONDS = 180             # random +/- spread added to every job run
DEFAULT_DAILY_REQUEST_LIMIT = 200
LOGIN_FAILURE_PAUSE_HOURS = 12   # auto-pause after repeated login failures
LOGIN_FAILURES_BEFORE_PAUSE = 2

SESSION_TTL_DAYS = 7
SESSION_COOKIE = "sniper_session"

# Luxmed session is reused across jobs; re-login only when older than this.
LUXMED_SESSION_MAX_AGE_MINUTES = 10

DICTIONARY_CACHE_TTL_HOURS = 24
