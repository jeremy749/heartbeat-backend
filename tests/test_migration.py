"""Opening an older database in place.

The regression here is real: init_db's backfill used to open a second connection
while the first still held the write lock, so a database with more than one
distinct orphan patient name blocked for the whole busy timeout and then raised
"database is locked" - the server would not start at all.
"""

from __future__ import annotations

import sqlite3

import pytest

LEGACY_BEATS = """
CREATE TABLE beats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    patient TEXT NOT NULL DEFAULT 'Demo User',
    class_code TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    is_abnormal INTEGER NOT NULL,
    alert_level TEXT NOT NULL DEFAULT 'green',
    alert_detail TEXT NOT NULL DEFAULT '',
    bpm REAL, flags TEXT, probabilities TEXT, samples TEXT,
    recorded_at TEXT NOT NULL
)
"""


def write_legacy_db(path, patients):
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_BEATS)
    for name in patients:
        conn.execute(
            "INSERT INTO beats (user_id, patient, class_code, classification, confidence,"
            " is_abnormal, recorded_at) VALUES (NULL, ?, 'N', 'Normal', 0.9, 0, ?)",
            (name, "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("patients", [["Alice"], ["Alice", "Bob", "Carol", "Dave"]])
def test_legacy_database_migrates(env, patients):
    from app import database as db

    write_legacy_db(db.db_path(), patients)
    db.init_db()  # used to hang, then raise OperationalError, for len(patients) > 1

    rows = db.get_history(limit=100)
    assert len(rows) == len(patients)
    assert all(r["user_id"] is not None for r in rows)
    assert {r["patient"] for r in rows} == set(patients)


def test_backfilled_rows_are_linked_to_the_right_user(env):
    from app import database as db

    write_legacy_db(db.db_path(), ["Alice", "Bob"])
    db.init_db()

    by_name = {r["patient"]: r["user_id"] for r in db.get_history(limit=100)}
    assert by_name["Alice"] == db.get_or_create_user("Alice")["id"]
    assert by_name["Bob"] == db.get_or_create_user("Bob")["id"]
    assert by_name["Alice"] != by_name["Bob"]


def test_init_db_is_idempotent(env):
    from app import database as db

    write_legacy_db(db.db_path(), ["Alice", "Bob"])
    db.init_db()
    db.init_db()  # a restart must not duplicate users or rows
    assert len(db.get_history(limit=100)) == 2
    assert len([u for u in db.list_users() if u["name"] in {"Alice", "Bob"}]) == 2


def test_wal_is_enabled(env):
    from app import database as db

    db.init_db()
    conn = db._connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_sessions_from_before_expiry_existed_keep_working(env):
    """Adding expires_at must not sign every existing user out."""
    from app import database as db

    db.init_db()
    user = db.get_or_create_user("Alice")

    # A session row as an older build wrote it: no expires_at.
    conn = sqlite3.connect(db.db_path())
    conn.execute("ALTER TABLE sessions DROP COLUMN expires_at")
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES ('legacy', ?, ?)",
        (user["id"], db.utc_iso()),
    )
    conn.commit()
    conn.close()

    db.init_db()  # re-runs the migration
    assert db.user_for_token("legacy") == {"id": user["id"], "name": "Alice"}
