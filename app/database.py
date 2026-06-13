"""
SQLite persistence for classified beats.

Uses the Python standard library only (sqlite3) so there are no heavy ORM
dependencies. The database file lives next to this package as heartbeat.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Defaults to heartbeat.db next to the package. Override with the HEARTBEAT_DB
# env var, e.g. if the project folder is on a network drive that doesn't
# support SQLite file locking (OneDrive, some VMs).
DB_PATH = Path(os.environ.get("HEARTBEAT_DB", Path(__file__).resolve().parent.parent / "heartbeat.db"))

DEFAULT_PATIENT = "Demo Patient"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the beats table if needed, then add any newer columns in place."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beats (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                patient        TEXT    NOT NULL DEFAULT 'Demo Patient',
                class_code     TEXT    NOT NULL,
                classification TEXT    NOT NULL,
                confidence     REAL    NOT NULL,
                is_abnormal    INTEGER NOT NULL,
                alert_level    TEXT    NOT NULL DEFAULT 'green',
                alert_detail   TEXT    NOT NULL DEFAULT '',
                bpm            REAL,
                flags          TEXT,           -- JSON array
                probabilities  TEXT,           -- JSON object
                samples        TEXT,           -- JSON array (optional)
                recorded_at    TEXT    NOT NULL
            )
            """
        )
        # Migrate older databases: add any columns that don't exist yet.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(beats)")}
        migrations = {
            "patient": "ALTER TABLE beats ADD COLUMN patient TEXT NOT NULL DEFAULT 'Demo Patient'",
            "alert_level": "ALTER TABLE beats ADD COLUMN alert_level TEXT NOT NULL DEFAULT 'green'",
            "alert_detail": "ALTER TABLE beats ADD COLUMN alert_detail TEXT NOT NULL DEFAULT ''",
        }
        for col, ddl in migrations.items():
            if col not in existing:
                conn.execute(ddl)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_beats_patient ON beats(patient)")
        conn.commit()


def insert_beat(
    *,
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
    """Insert a beat and return the stored row as a dict."""
    ts = (recorded_at or datetime.now(timezone.utc)).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO beats
                (patient, class_code, classification, confidence, is_abnormal,
                 alert_level, alert_detail, bpm, flags, probabilities, samples, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patient,
                class_code,
                classification,
                confidence,
                int(is_abnormal),
                alert_level,
                alert_detail,
                bpm,
                json.dumps(flags or []),
                json.dumps(probabilities or {}),
                json.dumps(samples) if samples else None,
                ts,
            ),
        )
        conn.commit()
        return _row_to_dict(
            conn.execute("SELECT * FROM beats WHERE id = ?", (cur.lastrowid,)).fetchone()
        )


def _build_filters(
    patient: Optional[str],
    class_filter: Optional[str],
    abnormal_only: bool,
    min_confidence: Optional[float],
    since: Optional[str],
    until: Optional[str],
):
    """Build a WHERE clause + params shared by history/export/count."""
    clauses: List[str] = []
    params: list = []
    if patient and patient != "All":
        clauses.append("patient = ?")
        params.append(patient)
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


def get_history(
    limit: int = 100,
    offset: int = 0,
    patient: Optional[str] = None,
    class_filter: Optional[str] = None,
    abnormal_only: bool = False,
    min_confidence: Optional[float] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[dict]:
    """Return beats newest-first with optional filters and pagination."""
    where, params = _build_filters(
        patient, class_filter, abnormal_only, min_confidence, since, until
    )
    query = f"SELECT * FROM beats{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    with _connect() as conn:
        rows = conn.execute(query, [*params, limit, offset]).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_history(
    patient: Optional[str] = None,
    class_filter: Optional[str] = None,
    abnormal_only: bool = False,
    min_confidence: Optional[float] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> int:
    where, params = _build_filters(
        patient, class_filter, abnormal_only, min_confidence, since, until
    )
    with _connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM beats{where}", params).fetchone()[0]


def get_recent(patient: Optional[str], n: int) -> List[dict]:
    """Most recent n beats (newest first), used for alert windowing."""
    where, params = _build_filters(patient, None, False, None, None, None)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT classification FROM beats{where} ORDER BY id DESC LIMIT ?",
            [*params, n],
        ).fetchall()
        return [{"classification": r["classification"]} for r in rows]


def get_latest(patient: Optional[str] = None) -> Optional[dict]:
    where, params = _build_filters(patient, None, False, None, None, None)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM beats{where} ORDER BY id DESC LIMIT 1", params
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_stats(patient: Optional[str] = None) -> dict:
    where, params = _build_filters(patient, None, False, None, None, None)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM beats{where}", params).fetchone()[0]
        abn_where = where + (" AND " if where else " WHERE ") + "is_abnormal = 1"
        abnormal = conn.execute(f"SELECT COUNT(*) FROM beats{abn_where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT classification, COUNT(*) c FROM beats{where} GROUP BY classification", params
        ).fetchall()
        counts = {r["classification"]: r["c"] for r in rows}
        latest = get_latest(patient)
    return {
        "total_beats": total,
        "abnormal_beats": abnormal,
        "counts_by_class": counts,
        "latest_bpm": latest["bpm"] if latest else None,
        "latest_classification": latest["classification"] if latest else None,
    }


def get_trends(patient: Optional[str] = None, points: int = 60) -> dict:
    """Heart-rate series + class/alert distributions for the Trends view."""
    where, params = _build_filters(patient, None, False, None, None, None)
    bpm_clause = (where + " AND " if where else " WHERE ") + "bpm IS NOT NULL"
    with _connect() as conn:
        # Heart-rate over time: last `points` beats that have a bpm, oldest first.
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
        "heart_rate": heart_rate,
        "class_distribution": {r["classification"]: r["c"] for r in class_rows},
        "alert_distribution": {r["alert_level"]: r["c"] for r in alert_rows},
    }


def list_patients() -> List[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT patient FROM beats ORDER BY patient"
        ).fetchall()
        return [r["patient"] for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
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
