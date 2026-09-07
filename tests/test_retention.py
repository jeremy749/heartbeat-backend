"""Retention: dropping old waveforms, and the optional beat window.

A beat's 200-sample waveform dominates its storage - roughly 1.5 KB against a
couple of hundred bytes of metadata - so the waveforms go early while the beats
themselves are kept by default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import auth, signup

SAMPLES = [round(i / 200, 4) for i in range(200)]


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def post(client, token, **over):
    body = {"class_code": "N", "confidence": 0.9, "bpm": 70, "samples": SAMPLES}
    r = client.post("/api/beats", json={**body, **over}, headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def stored_samples(user_id=None):
    """Read samples straight from the table - the API never returns them."""
    from app import database as db

    conn = db._connect()
    try:
        sql = "SELECT id, samples FROM beats"
        rows = conn.execute(sql).fetchall()
        return {r["id"]: r["samples"] for r in rows}
    finally:
        conn.close()


def test_old_waveforms_are_dropped(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    old = post(client, user["token"], recorded_at=ago(hours=48))
    new = post(client, user["token"], recorded_at=ago(hours=1))

    assert db.prune_samples(older_than_hours=24) == 1

    kept = stored_samples()
    assert kept[old["id"]] is None
    assert kept[new["id"]] is not None


def test_pruning_keeps_the_beat_and_its_metadata(client, env):
    """Only the waveform goes - the row stays queryable."""
    from app import database as db

    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at=ago(hours=48), confidence=0.83, bpm=64)
    db.prune_samples(older_than_hours=24)

    rows = client.get("/api/history", headers=auth(user["token"])).json()
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.83
    assert rows[0]["bpm"] == 64
    assert rows[0]["classification"] == "Normal"


def test_pruning_is_idempotent(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at=ago(hours=48))

    assert db.prune_samples(older_than_hours=24) == 1
    assert db.prune_samples(older_than_hours=24) == 0  # nothing left to clear


def test_pruning_is_disabled_by_a_zero_window(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    old = post(client, user["token"], recorded_at=ago(days=400))

    assert db.prune_samples(older_than_hours=0) == 0
    assert stored_samples()[old["id"]] is not None


def test_strip_view_still_works_after_pruning(client, env):
    """The strip reads recent beats, so pruning old ones must not break it."""
    from app import database as db

    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at=ago(hours=48))
    post(client, user["token"], recorded_at=ago(minutes=5))
    db.prune_samples(older_than_hours=24)

    strip = client.get("/api/strip", headers=auth(user["token"])).json()
    assert strip["beats"] == 1
    assert len(strip["samples"]) == len(SAMPLES)


def test_beats_are_kept_forever_by_default(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at=ago(days=3650))

    assert db.BEAT_RETENTION_DAYS == 0
    assert db.delete_old_beats() == 0
    assert len(client.get("/api/history", headers=auth(user["token"])).json()) == 1


def test_beat_window_removes_old_beats_when_configured(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at=ago(days=40))
    post(client, user["token"], recorded_at=ago(days=1))

    assert db.delete_old_beats(older_than_days=30) == 1
    assert len(client.get("/api/history", headers=auth(user["token"])).json()) == 1


def test_retention_sweep_reports_what_it_did(client, env):
    from app import database as db
    from app.main import run_retention

    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at=ago(hours=48))
    db.create_session(user["id"], ttl_hours=-1)

    result = run_retention()
    assert result["waveforms_dropped"] == 1
    assert result["sessions_purged"] == 1
    assert result["beats_removed"] == 0  # off by default


def test_vacuum_runs(client, env):
    """Space reclamation is manual, but it must at least work."""
    from app import database as db

    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    db.vacuum()
    assert len(client.get("/api/history", headers=auth(user["token"])).json()) == 1
