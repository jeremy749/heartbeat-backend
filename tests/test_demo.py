"""The demo seeder.

It exists because a free host wipes the database on every cold start, and a
shared link that opens on an empty dashboard has already lost the person who
clicked it. What it writes has to look like a real recording and go through the
real alert engine, or the demo shows features working on data that could never
occur.
"""

from __future__ import annotations

from conftest import auth, signup


def test_seeding_is_off_unless_asked_for(client, env):
    """A local run must not invent readings behind the developer's back."""
    from app import database as db

    user = db.get_or_create_user(db.DEFAULT_USER)
    assert db.count_beats(user["id"]) == 0


def test_seeding_fills_the_demo_account(client, env):
    from app import database as db
    from app.demo import seed_demo_data

    written = seed_demo_data()
    assert written > 0

    user = db.get_or_create_user(db.DEFAULT_USER)
    assert db.count_beats(user["id"]) == written


def test_seeding_twice_does_not_double_the_data(client, env):
    """A host that keeps its disk must not gain six hours on every restart."""
    from app.demo import seed_demo_data

    first = seed_demo_data()
    assert seed_demo_data() == 0

    from app import database as db

    user = db.get_or_create_user(db.DEFAULT_USER)
    assert db.count_beats(user["id"]) == first


def test_the_demo_recording_is_reproducible(client, env):
    from app import database as db
    from app.demo import build_beats

    a = build_beats(1, "Demo User", count=50)
    b = build_beats(1, "Demo User", count=50)
    assert [r["class_code"] for r in a] == [r["class_code"] for r in b]
    assert [r["bpm"] for r in a] == [r["bpm"] for r in b]
    assert db  # keeps the import honest


def test_every_class_shows_up(client, env):
    """The class chart and the type filter need something in each bucket."""
    from app.demo import build_beats

    codes = {r["class_code"] for r in build_beats(1, "Demo User")}
    assert codes == {"N", "S", "V", "F", "Q"}


def test_enough_abnormal_beats_to_demonstrate_the_features(client, env):
    """At a clinically typical rate the abnormal filter comes back empty."""
    from app.demo import build_beats

    rows = build_beats(1, "Demo User")
    abnormal = [r for r in rows if r["is_abnormal"]]
    assert 0.01 < len(abnormal) / len(rows) < 0.15


def test_alert_levels_come_from_the_real_engine(client, env):
    """Not hardcoded: a confident ventricular beat must read red here too."""
    from app.demo import build_beats

    rows = build_beats(1, "Demo User")
    levels = {r["alert_level"] for r in rows}
    assert "green" in levels
    assert "red" in levels  # the ventricular runs escalate

    for r in rows:
        assert r["alert_detail"].strip()
        if r["classification"] == "Normal" and r["confidence"] >= 0.6:
            assert r["alert_level"] != "red"  # a normal beat is never urgent


def test_only_recent_beats_carry_a_waveform(client, env):
    """Matching what retention would have left behind on a real recording."""
    from app.demo import DEMO_WAVEFORM_BEATS, build_beats

    rows = build_beats(1, "Demo User")
    with_samples = [r for r in rows if r["samples"]]
    assert len(with_samples) == DEMO_WAVEFORM_BEATS
    assert all(r["samples"] for r in rows[-DEMO_WAVEFORM_BEATS:])
    assert all(len(r["samples"]) == 200 for r in with_samples)


def test_timestamps_run_forwards_and_end_about_now(client, env):
    from datetime import datetime, timezone

    from app.demo import build_beats

    rows = build_beats(1, "Demo User", count=100)
    times = [r["recorded_at"] for r in rows]
    assert times == sorted(times)

    gap = (datetime.now(timezone.utc) - times[-1]).total_seconds()
    assert abs(gap) < 60  # the newest beat is the present one


def test_heart_rates_stay_plausible(client, env):
    from app.demo import build_beats

    bpms = [r["bpm"] for r in build_beats(1, "Demo User")]
    assert all(45 <= b <= 150 for b in bpms)
    assert len(set(bpms)) > 50  # varies, so HRV is not flat


def test_the_seeded_account_serves_the_whole_dashboard(client, env):
    """The point of seeding: every view has something to show."""
    from app.demo import seed_demo_data

    seed_demo_data()
    user = signup(client, "Demo User", "demo")
    h = auth(user["token"])

    stats = client.get("/api/stats", headers=h).json()
    assert stats["total_beats"] > 0
    assert stats["abnormal_beats"] > 0

    trends = client.get("/api/trends", headers=h).json()
    assert trends["points"] > 0
    assert len(trends["class_distribution"]) > 1

    strip = client.get("/api/strip", headers=h).json()
    assert strip["beats"] > 0
    assert strip["hrv"]["rmssd"] is not None  # HRV needs varying intervals

    assert len(client.get("/api/history", headers=h).json()) > 0
    assert client.get("/api/report.pdf", headers=h).status_code == 200
