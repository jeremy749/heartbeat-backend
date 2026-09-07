"""
SQLite persistence for users and classified beats.

Standard-library sqlite3 only (no ORM). Each reading is linked to a user by a
numeric user_id (not by name), so two people who happen to share a name stay
separate and a user can be renamed without breaking their history.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = Path(os.environ.get("HEARTBEAT_DB", Path(__file__).resolve().parent.parent / "heartbeat.db"))

DEFAULT_USER = "Demo User"
DEFAULT_USER_PASSWORD = "demo"  # the seeded demo account's password


# ── Password hashing (stdlib only) ────────────────────────────────────────────
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def _new_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    return salt, _hash_password(password, salt)


BUSY_TIMEOUT_MS = 5000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    # WAL lets readers keep working while a writer holds the lock. FastAPI runs
    # these sync functions in a threadpool, so without it two overlapping
    # requests raise "database is locked". busy_timeout makes a writer wait its
    # turn instead of failing instantly.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db():
    """Open a connection, commit on success, roll back on error, always close.

    sqlite3's own `with conn:` is a *transaction* manager - it commits, but it
    never closes. Using it directly leaked one connection per request.
    """
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if needed and migrate older databases in place."""
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL UNIQUE,
                salt          TEXT,
                password_hash TEXT,
                created_at    TEXT    NOT NULL
            )
            """
        )
        # Older user tables won't have the password columns yet.
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "salt" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN salt TEXT")
        if "password_hash" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beats (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER,
                patient        TEXT    NOT NULL DEFAULT 'Demo User',
                class_code     TEXT    NOT NULL,
                classification TEXT    NOT NULL,
                confidence     REAL    NOT NULL,
                is_abnormal    INTEGER NOT NULL,
                alert_level    TEXT    NOT NULL DEFAULT 'green',
                alert_detail   TEXT    NOT NULL DEFAULT '',
                bpm            REAL,
                flags          TEXT,
                probabilities  TEXT,
                samples        TEXT,
                recorded_at    TEXT    NOT NULL
            )
            """
        )
        # Add columns that older databases won't have yet.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(beats)")}
        for col, ddl in {
            "user_id": "ALTER TABLE beats ADD COLUMN user_id INTEGER",
            "patient": "ALTER TABLE beats ADD COLUMN patient TEXT NOT NULL DEFAULT 'Demo User'",
            "alert_level": "ALTER TABLE beats ADD COLUMN alert_level TEXT NOT NULL DEFAULT 'green'",
            "alert_detail": "ALTER TABLE beats ADD COLUMN alert_detail TEXT NOT NULL DEFAULT ''",
        }.items():
            if col not in existing:
                conn.execute(ddl)
        conn.commit()

        # Backfill: give any reading without a user_id one, derived from its
        # patient name (so old name-based rows become id-linked). Resolved on
        # *this* connection - opening a second one here would block against the
        # write lock this transaction already holds, then time out.
        now = datetime.now(timezone.utc).isoformat()
        orphan_names = [
            r["patient"]
            for r in conn.execute(
                "SELECT DISTINCT patient FROM beats WHERE user_id IS NULL"
            )
        ]
        for name in orphan_names:
            uname = (name or DEFAULT_USER).strip() or DEFAULT_USER
            conn.execute(
                "INSERT OR IGNORE INTO users (name, created_at) VALUES (?, ?)", (uname, now)
            )
            uid = conn.execute(
                "SELECT id FROM users WHERE name = ?", (uname,)
            ).fetchone()["id"]
            conn.execute(
                "UPDATE beats SET user_id = ? WHERE user_id IS NULL AND patient = ?",
                (uid, name),
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_beats_user ON beats(user_id)")
        conn.commit()

    # Seed the demo account so the demo works out of the box (Demo User / demo).
    _ensure_password(DEFAULT_USER, DEFAULT_USER_PASSWORD)


# ── Users ─────────────────────────────────────────────────────────────────────
def get_or_create_user(name: str) -> dict:
    """Return {id, name} for this name, creating the user if needed."""
    name = (name or DEFAULT_USER).strip() or DEFAULT_USER
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (name, created_at) VALUES (?, ?)", (name, now)
        )
        conn.commit()
        row = conn.execute("SELECT id, name FROM users WHERE name = ?", (name,)).fetchone()
        return {"id": row["id"], "name": row["name"]}


def get_user(user_id: int) -> Optional[dict]:
    """Return {id, name} for an existing user id, or None if there is no such user."""
    with _db() as conn:
        row = conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
        return {"id": row["id"], "name": row["name"]} if row else None


def list_users() -> List[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT id, name FROM users ORDER BY name").fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows]


def _ensure_password(name: str, password: str) -> None:
    """Create the user if missing and give it a password only if it has none."""
    user = get_or_create_user(name)
    with _db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if row and not row["password_hash"]:
            salt, ph = _new_password(password)
            conn.execute("UPDATE users SET salt = ?, password_hash = ? WHERE id = ?", (salt, ph, user["id"]))
            conn.commit()


def authenticate(name: str, password: str) -> Optional[dict]:
    """Sign in or sign up.

    - New name: creates the account with this password.
    - Existing account without a password yet: sets it (one-time migration).
    - Existing account with a password: must match, else returns None.
    """
    name = (name or "").strip()
    if not name or not password:
        return None
    user = get_or_create_user(name)
    with _db() as conn:
        row = conn.execute(
            "SELECT salt, password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if not row["password_hash"]:
            salt, ph = _new_password(password)
            conn.execute("UPDATE users SET salt = ?, password_hash = ? WHERE id = ?", (salt, ph, user["id"]))
            conn.commit()
            return user
        if secrets.compare_digest(_hash_password(password, row["salt"]), row["password_hash"]):
            return user
        return None


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with _db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return token


def user_for_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.name FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
        return {"id": row["id"], "name": row["name"]} if row else None


def delete_session(token: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def get_or_create_device_key() -> str:
    """The shared secret devices present to POST beats.

    Generated once and stored, so it survives restarts. Overridden by the
    DEVICE_API_KEY environment variable when that is set (see app.main).
    """
    with _db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'device_api_key'").fetchone()
        if row:
            return row["value"]
        key = secrets.token_urlsafe(24)
        conn.execute("INSERT INTO meta (key, value) VALUES ('device_api_key', ?)", (key,))
        return key


# ── Account management ────────────────────────────────────────────────────────
def get_account(user_id: int) -> Optional[dict]:
    """Account summary: name, when it was created, and how many readings exist."""
    with _db() as conn:
        u = conn.execute(
            "SELECT id, name, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not u:
            return None
        count = conn.execute(
            "SELECT COUNT(*) FROM beats WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        return {"id": u["id"], "name": u["name"], "created_at": u["created_at"], "reading_count": count}


def change_password(user_id: int, current: str, new: str) -> bool:
    """Change a password after checking the current one. Returns False if wrong."""
    if not new:
        return False
    with _db() as conn:
        row = conn.execute(
            "SELECT salt, password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return False
        if row["password_hash"] and not secrets.compare_digest(
            _hash_password(current, row["salt"]), row["password_hash"]
        ):
            return False
        salt, ph = _new_password(new)
        conn.execute("UPDATE users SET salt = ?, password_hash = ? WHERE id = ?", (salt, ph, user_id))
        conn.commit()
        return True


def delete_user_readings(user_id: int) -> int:
    """Delete all of a user's beats (keep the account). Returns rows removed."""
    with _db() as conn:
        cur = conn.execute("DELETE FROM beats WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount


def delete_user(user_id: int) -> None:
    """Delete a user, their readings, and their sessions entirely."""
    with _db() as conn:
        conn.execute("DELETE FROM beats WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


# ── ECG strip replay + HRV inputs ─────────────────────────────────────────────
def get_recent_with_samples(user_id: int, n: int) -> List[dict]:
    """Most recent n beats that carry sample windows, oldest first."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT samples, bpm, recorded_at FROM beats "
            "WHERE user_id = ? AND samples IS NOT NULL ORDER BY id DESC LIMIT ?",
            (user_id, n),
        ).fetchall()
        return [
            {"samples": json.loads(r["samples"]) if r["samples"] else [], "bpm": r["bpm"], "recorded_at": r["recorded_at"]}
            for r in reversed(rows)
        ]


def get_bpm_series(user_id: int, n: int = 200) -> List[float]:
    """Recent heart-rate values (oldest first) for HRV calculation."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT bpm FROM beats WHERE user_id = ? AND bpm IS NOT NULL ORDER BY id DESC LIMIT ?",
            (user_id, n),
        ).fetchall()
        return [r["bpm"] for r in reversed(rows)]


# ── Beats ─────────────────────────────────────────────────────────────────────
def insert_beat(
    *,
    user_id: int,
    patient: str,
    class_code: str,
    classification: str,
    confidence: float,
    is_abnormal: bool,
    alert_level: str,
    alert_detail: str,
    bpm: Optional[float],
    flags: List[str],
    probabilities: Dict[str, float],
    samples: Optional[List[float]],
    recorded_at: Optional[datetime],
) -> dict:
    ts = (recorded_at or datetime.now(timezone.utc)).isoformat()
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO beats
                (user_id, patient, class_code, classification, confidence, is_abnormal,
                 alert_level, alert_detail, bpm, flags, probabilities, samples, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, patient, class_code, classification, confidence, int(is_abnormal),
                alert_level, alert_detail, bpm,
                json.dumps(flags or []), json.dumps(probabilities or {}),
                json.dumps(samples) if samples else None, ts,
            ),
        )
        conn.commit()
        return _row_to_dict(
            conn.execute("SELECT * FROM beats WHERE id = ?", (cur.lastrowid,)).fetchone()
        )


def _filters(user_id, class_filter, abnormal_only, min_confidence, since, until):
    clauses: List[str] = []
    params: list = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if class_filter and class_filter != "All":
        clauses.append("classification = ?")
        params.append(class_filter)
    if abnormal_only:
        clauses.append("is_abnormal = 1")
    if min_confidence is not None:
        clauses.append("confidence >= ?")
        params.append(min_confidence)
    if since:
        clauses.append("recorded_at >= ?")
        params.append(since)
    if until:
        clauses.append("recorded_at <= ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_history(limit=100, offset=0, user_id=None, class_filter=None,
                abnormal_only=False, min_confidence=None, since=None, until=None):
    where, params = _filters(user_id, class_filter, abnormal_only, min_confidence, since, until)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM beats{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_recent(user_id, n):
    where, params = _filters(user_id, None, False, None, None, None)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT classification FROM beats{where} ORDER BY id DESC LIMIT ?",
            [*params, n],
        ).fetchall()
        return [{"classification": r["classification"]} for r in rows]


def get_latest(user_id=None):
    where, params = _filters(user_id, None, False, None, None, None)
    with _db() as conn:
        row = conn.execute(
            f"SELECT * FROM beats{where} ORDER BY id DESC LIMIT 1", params
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_stats(user_id=None):
    where, params = _filters(user_id, None, False, None, None, None)
    with _db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM beats{where}", params).fetchone()[0]
        abn_where = where + (" AND " if where else " WHERE ") + "is_abnormal = 1"
        abnormal = conn.execute(f"SELECT COUNT(*) FROM beats{abn_where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT classification, COUNT(*) c FROM beats{where} GROUP BY classification", params
        ).fetchall()
        counts = {r["classification"]: r["c"] for r in rows}
        latest = get_latest(user_id)
    return {
        "total_beats": total,
        "abnormal_beats": abnormal,
        "counts_by_class": counts,
        "latest_bpm": latest["bpm"] if latest else None,
        "latest_classification": latest["classification"] if latest else None,
    }


def get_trends(user_id=None, points=60):
    where, params = _filters(user_id, None, False, None, None, None)
    bpm_clause = (where + " AND " if where else " WHERE ") + "bpm IS NOT NULL"
    with _db() as conn:
        hr_rows = conn.execute(
            f"SELECT recorded_at, bpm FROM beats{bpm_clause} ORDER BY id DESC LIMIT ?",
            [*params, points],
        ).fetchall()
        heart_rate = [{"t": r["recorded_at"], "bpm": r["bpm"]} for r in reversed(hr_rows)]
        class_rows = conn.execute(
            f"SELECT classification, COUNT(*) c FROM beats{where} GROUP BY classification", params
        ).fetchall()
        alert_rows = conn.execute(
            f"SELECT alert_level, COUNT(*) c FROM beats{where} GROUP BY alert_level", params
        ).fetchall()
    return {
        "points": len(heart_rate),
        "heart_rate": heart_rate,
        "class_distribution": {r["classification"]: r["c"] for r in class_rows},
        "alert_distribution": {r["alert_level"]: r["c"] for r in alert_rows},
    }


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "patient": row["patient"],
        "class_code": row["class_code"],
        "classification": row["classification"],
        "confidence": row["confidence"],
        "is_abnormal": bool(row["is_abnormal"]),
        "alert_level": row["alert_level"],
        "alert_detail": row["alert_detail"],
        "bpm": row["bpm"],
        "flags": json.loads(row["flags"]) if row["flags"] else [],
        "probabilities": json.loads(row["probabilities"]) if row["probabilities"] else {},
        "recorded_at": row["recorded_at"],
    }
