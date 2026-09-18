"""History filters, timestamp handling, and the export endpoints."""

from __future__ import annotations

from conftest import BEAT, auth, signup, ticket


def post(client, token, **over):
    r = client.post("/api/beats", json=dict(BEAT, **over), headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_timestamps_are_stored_as_utc_whatever_offset_arrives(client):
    """Rows are compared as text in SQL, so mixed offsets must be normalized.

    These two beats are the same instant written three ways; all must land on
    the same stored timestamp, or since/until silently return the wrong rows.
    """
    user = signup(client, "Alice", "pw")
    same_instant = [
        "2026-03-01T12:00:00+00:00",
        "2026-03-01T17:00:00+05:00",
        "2026-03-01T07:00:00-05:00",
    ]
    stored = {post(client, user["token"], recorded_at=t)["recorded_at"] for t in same_instant}
    assert len(stored) == 1, stored


def test_since_filter_understands_a_non_utc_bound(client):
    """A bound written in another offset must be converted before comparing.

    Compared as raw text, "2026-03-01T20:00:00-05:00" sorts below
    "2026-03-02T00:00:00+00:00" and the later beat wrongly matches. Converted,
    the bound is 2026-03-02T01:00:00Z - after both beats - so nothing does.
    """
    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at="2026-03-01T00:00:00+00:00", class_code="N")
    post(client, user["token"], recorded_at="2026-03-02T00:00:00+00:00", class_code="V")

    rows = client.get(
        "/api/history", params={"since": "2026-03-01T20:00:00-05:00"}, headers=auth(user["token"])
    ).json()
    assert [r["class_code"] for r in rows] == []


def test_since_filter_keeps_beats_after_a_non_utc_bound(client):
    """The other direction: 2026-03-01T07:00:00-05:00 is noon UTC on the 1st."""
    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at="2026-03-01T00:00:00+00:00", class_code="N")
    post(client, user["token"], recorded_at="2026-03-02T00:00:00+00:00", class_code="V")

    rows = client.get(
        "/api/history", params={"since": "2026-03-01T07:00:00-05:00"}, headers=auth(user["token"])
    ).json()
    assert [r["class_code"] for r in rows] == ["V"]


def test_since_filter_accepts_a_trailing_z(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at="2026-03-02T00:00:00+00:00")
    rows = client.get(
        "/api/history", params={"since": "2026-03-01T00:00:00Z"}, headers=auth(user["token"])
    ).json()
    assert len(rows) == 1


def test_until_filter_excludes_later_beats(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"], recorded_at="2026-03-01T00:00:00+00:00", class_code="N")
    post(client, user["token"], recorded_at="2026-03-05T00:00:00+00:00", class_code="V")

    rows = client.get(
        "/api/history", params={"until": "2026-03-02T00:00:00Z"}, headers=auth(user["token"])
    ).json()
    assert [r["class_code"] for r in rows] == ["N"]


def test_a_malformed_timestamp_is_a_400_not_a_500(client):
    user = signup(client, "Alice", "pw")
    r = client.get("/api/history", params={"since": "last tuesday"}, headers=auth(user["token"]))
    assert r.status_code == 400


def test_abnormal_only_and_class_filters(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"], class_code="N")
    post(client, user["token"], class_code="V", confidence=0.95)

    only_abnormal = client.get(
        "/api/history", params={"abnormal_only": True}, headers=auth(user["token"])
    ).json()
    assert [r["class_code"] for r in only_abnormal] == ["V"]

    normals = client.get(
        "/api/history", params={"type": "Normal"}, headers=auth(user["token"])
    ).json()
    assert [r["class_code"] for r in normals] == ["N"]


def test_pagination(client):
    user = signup(client, "Alice", "pw")
    for _ in range(5):
        post(client, user["token"])

    first = client.get("/api/history", params={"limit": 2}, headers=auth(user["token"])).json()
    second = client.get(
        "/api/history", params={"limit": 2, "offset": 2}, headers=auth(user["token"])
    ).json()
    assert len(first) == len(second) == 2
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})


def test_stats_count_abnormal_beats(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"], class_code="N")
    post(client, user["token"], class_code="V", confidence=0.95)

    s = client.get("/api/stats", headers=auth(user["token"])).json()
    assert s["total_beats"] == 2
    assert s["abnormal_beats"] == 1


def test_csv_export_is_scoped_and_human_readable(client):
    alice = signup(client, "Alice", "pw")
    bob = signup(client, "Bob", "pw")
    post(client, alice["token"], confidence=0.97)
    post(client, bob["token"])

    body = client.get("/api/export.csv", headers=auth(alice["token"])).text
    assert "Bob" not in body
    assert "97%" in body  # confidence rendered for humans, not as 0.97
    assert len(body.strip().splitlines()) == 2  # header + Alice's single beat


def test_csv_export_works_from_a_plain_link(client):
    """Download links cannot set headers, so a one-use ticket rides in the query."""
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    r = client.get("/api/export.csv", params={"ticket": ticket(client, user["token"])})
    assert r.status_code == 200


def test_a_session_token_in_the_url_no_longer_works(client):
    """The whole point: a URL is logged, so it must not carry a 30-day credential."""
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    assert client.get("/api/export.csv", params={"token": user["token"]}).status_code == 401
    assert client.get("/api/history", params={"token": user["token"]}).status_code == 401


def test_pdf_report_is_generated(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    r = client.get("/api/report.pdf", headers=auth(user["token"]))
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"


def test_read_endpoints_require_a_token(client):
    for path in ["/api/history", "/api/stats", "/api/latest", "/api/trends", "/api/strip"]:
        assert client.get(path).status_code == 401, path
