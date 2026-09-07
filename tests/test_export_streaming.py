"""The CSV export streams rather than buffering the whole history."""

from __future__ import annotations

import csv
import io

from conftest import auth, signup


def post(client, token, **over):
    body = {"class_code": "N", "confidence": 0.9, "bpm": 70}
    r = client.post("/api/beats", json={**body, **over}, headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_export_emits_the_header_before_reading_any_row(client, env, monkeypatch):
    """The point of streaming: bytes go out before the history has been read.

    Asserted against the response's own iterator rather than the HTTP client -
    TestClient joins the body back into one chunk, so it cannot tell a streamed
    response from a buffered one.
    """
    import asyncio

    from starlette.responses import StreamingResponse

    from app import database as db
    from app import main

    user = signup(client, "Alice", "pw")
    for _ in range(200):
        post(client, user["token"])

    read_rows = []
    real_iter = db.iter_history

    def counting_iter(*args, **kwargs):
        for row in real_iter(*args, **kwargs):
            read_rows.append(row["id"])
            yield row

    monkeypatch.setattr(db, "iter_history", counting_iter)

    response = main.export_csv(
        type=None,
        abnormal_only=False,
        min_confidence=None,
        since=None,
        until=None,
        user={"id": user["id"], "name": "Alice"},
    )
    assert isinstance(response, StreamingResponse)

    async def first_chunk():
        return await response.body_iterator.__anext__()

    chunk = asyncio.run(first_chunk())
    text = chunk.decode() if isinstance(chunk, bytes) else chunk

    assert text.startswith("ID,Patient")
    assert read_rows == []  # not one row touched to produce the header


def test_export_contains_every_row(client):
    user = signup(client, "Alice", "pw")
    for _ in range(200):
        post(client, user["token"])

    body = client.get("/api/export.csv", headers=auth(user["token"])).text
    assert len(list(csv.reader(io.StringIO(body)))) == 201  # header + 200 beats


def test_export_content_is_well_formed(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"], confidence=0.97, bpm=72)
    post(client, user["token"], class_code="V", confidence=0.91, bpm=133, flags=["TACHY"])

    body = client.get("/api/export.csv", headers=auth(user["token"])).text
    rows = list(csv.reader(io.StringIO(body)))

    assert rows[0] == [
        "ID", "Patient", "Recorded At", "Classification", "Confidence",
        "Abnormal", "Alert Level", "Heart Rate (bpm)", "Flags",
    ]
    assert len(rows) == 3  # header + two beats

    newest = dict(zip(rows[0], rows[1]))  # history is newest-first
    assert newest["Classification"] == "Ventricular"
    assert newest["Confidence"] == "91%"
    assert newest["Abnormal"] == "Yes"
    assert newest["Alert Level"] == "Red"
    assert newest["Flags"] == "TACHY"


def test_export_respects_filters(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"], class_code="N")
    post(client, user["token"], class_code="V", confidence=0.95)

    body = client.get(
        "/api/export.csv", params={"abnormal_only": True}, headers=auth(user["token"])
    ).text
    rows = list(csv.reader(io.StringIO(body)))
    assert len(rows) == 2
    assert rows[1][3] == "Ventricular"


def test_export_is_scoped_to_the_signed_in_user(client):
    alice = signup(client, "Alice", "pw")
    bob = signup(client, "Bob", "pw")
    post(client, alice["token"])
    for _ in range(50):
        post(client, bob["token"])

    body = client.get("/api/export.csv", headers=auth(alice["token"])).text
    assert "Bob" not in body
    assert len(list(csv.reader(io.StringIO(body)))) == 2


def test_export_of_an_empty_history_is_just_the_header(client):
    user = signup(client, "Alice", "pw")
    body = client.get("/api/export.csv", headers=auth(user["token"])).text
    assert list(csv.reader(io.StringIO(body))) == [
        ["ID", "Patient", "Recorded At", "Classification", "Confidence",
         "Abnormal", "Alert Level", "Heart Rate (bpm)", "Flags"]
    ]


def test_iter_history_matches_get_history(client, env):
    """The streaming reader and the list reader must agree."""
    from app import database as db

    user = signup(client, "Alice", "pw")
    for i in range(30):
        post(client, user["token"], class_code="V" if i % 3 else "N", confidence=0.95)

    listed = db.get_history(limit=1000, user_id=user["id"])
    streamed = list(db.iter_history(user_id=user["id"], batch_size=7))
    assert [r["id"] for r in listed] == [r["id"] for r in streamed]


def test_iter_history_pages_through_every_row(client, env):
    """A batch size that does not divide the total must not drop the remainder."""
    from app import database as db

    user = signup(client, "Alice", "pw")
    for _ in range(25):
        post(client, user["token"])

    assert len(list(db.iter_history(user_id=user["id"], batch_size=10))) == 25
