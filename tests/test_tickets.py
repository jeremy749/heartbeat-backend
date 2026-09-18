"""Download tickets.

A browser cannot put an Authorization header on a download link or a WebSocket
handshake, so something must travel in the URL - and URLs are written to the
access log, the browser history and any proxy in between. Before tickets that
something was the session token, valid for thirty days. A ticket is what goes
there now: seconds long, one use, and spent the moment it is redeemed.
"""

from __future__ import annotations

from conftest import BEAT, auth, signup, ticket


def post(client, token, **over):
    r = client.post("/api/beats", json=dict(BEAT, **over), headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_issuing_a_ticket_needs_a_session(client):
    assert client.post("/api/ticket").status_code == 401
    assert client.post("/api/ticket", headers=auth("garbage")).status_code == 401


def test_a_ticket_reports_its_lifetime(client):
    from app import database as db

    user = signup(client, "Alice", "pw")
    body = client.post("/api/ticket", headers=auth(user["token"])).json()
    assert body["ticket"]
    assert body["expires_in"] == db.TICKET_TTL_SECONDS


def test_a_ticket_is_shorter_lived_than_a_session(client):
    """If it outlived the session token it would be no improvement at all."""
    from app import database as db

    assert db.TICKET_TTL_SECONDS < db.SESSION_TTL_HOURS * 3600


def test_a_ticket_downloads_the_csv(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    r = client.get("/api/export.csv", params={"ticket": ticket(client, user["token"])})
    assert r.status_code == 200
    assert "Alice" in r.text


def test_a_ticket_downloads_the_pdf(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    r = client.get("/api/report.pdf", params={"ticket": ticket(client, user["token"])})
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"


def test_a_ticket_is_spent_after_one_use(client):
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    tkt = ticket(client, user["token"])

    assert client.get("/api/export.csv", params={"ticket": tkt}).status_code == 200
    assert client.get("/api/export.csv", params={"ticket": tkt}).status_code == 401


def test_an_expired_ticket_is_refused(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    stale = db.create_ticket(user["id"], ttl_seconds=-1)
    assert client.get("/api/export.csv", params={"ticket": stale}).status_code == 401


def test_a_ticket_only_reaches_its_own_owners_data(client):
    alice = signup(client, "Alice", "pw")
    bob = signup(client, "Bob", "pw")
    post(client, alice["token"])
    post(client, bob["token"])

    body = client.get(
        "/api/export.csv", params={"ticket": ticket(client, alice["token"])}
    ).text
    assert "Alice" in body
    assert "Bob" not in body


def test_a_session_token_is_not_a_ticket(client):
    """Otherwise nothing would stop the old, logged URL from still working."""
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    assert client.get("/api/export.csv", params={"ticket": user["token"]}).status_code == 401


def test_deleting_an_account_invalidates_its_tickets(client):
    user = signup(client, "Alice", "pw")
    tkt = ticket(client, user["token"])
    assert client.delete("/api/account", headers=auth(user["token"])).status_code == 200
    assert client.get("/api/export.csv", params={"ticket": tkt}).status_code == 401


def test_the_header_still_works_for_downloads(client):
    """Anything that can set a header should not need a ticket at all."""
    user = signup(client, "Alice", "pw")
    post(client, user["token"])
    assert client.get("/api/export.csv", headers=auth(user["token"])).status_code == 200


def test_expired_tickets_are_swept_up(client, env):
    from app import database as db
    from app.main import run_retention

    user = signup(client, "Alice", "pw")
    db.create_ticket(user["id"], ttl_seconds=-1)
    db.create_ticket(user["id"], ttl_seconds=-5)
    live = db.create_ticket(user["id"])

    assert run_retention()["tickets_purged"] == 2
    assert db.redeem_ticket(live) is not None  # the good one survived


def test_ingest_no_longer_accepts_a_token_in_the_url(client):
    """A device can always set a header, so nothing needs one in the query."""
    user = signup(client, "Alice", "pw")
    r = client.post("/api/beats", json=BEAT, params={"token": user["token"]})
    assert r.status_code == 401
