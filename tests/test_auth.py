"""Sign-in, sessions, and the login throttle."""

from __future__ import annotations

import pytest

from conftest import auth, signup


def test_first_use_of_a_name_creates_the_account(client):
    body = signup(client, "Alice", "pw")
    assert body["created"] is True
    assert body["name"] == "Alice"
    assert body["token"]


def test_signing_in_again_does_not_report_a_new_account(client):
    signup(client, "Alice", "pw")
    assert signup(client, "Alice", "pw")["created"] is False


def test_wrong_password_is_rejected(client):
    signup(client, "Alice", "pw")
    r = client.post("/api/login", json={"name": "Alice", "password": "nope"})
    assert r.status_code == 401


def test_wrong_password_does_not_change_the_account(client):
    """A failed attempt must not overwrite the stored password."""
    signup(client, "Alice", "pw")
    client.post("/api/login", json={"name": "Alice", "password": "guess"})
    assert signup(client, "Alice", "pw")["token"]


def test_token_identifies_the_user(client):
    tok = signup(client, "Alice", "pw")["token"]
    r = client.get("/api/me", headers=auth(tok))
    assert r.status_code == 200 and r.json()["name"] == "Alice"


def test_unknown_token_is_rejected(client):
    assert client.get("/api/me", headers=auth("garbage")).status_code == 401


def test_logout_invalidates_the_token(client):
    tok = signup(client, "Alice", "pw")["token"]
    assert client.post("/api/logout", headers=auth(tok)).status_code == 200
    assert client.get("/api/me", headers=auth(tok)).status_code == 401


def test_expired_session_is_rejected(client, env):
    """A token past its expiry stops working and is cleaned up."""
    from app import database as db

    user = signup(client, "Alice", "pw")
    stale = db.create_session(user["id"], ttl_hours=-1)  # already expired
    assert client.get("/api/me", headers=auth(stale)).status_code == 401
    assert db.user_for_token(stale) is None


def test_purge_expired_sessions_removes_only_stale_ones(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    live = db.create_session(user["id"])
    db.create_session(user["id"], ttl_hours=-1)
    db.create_session(user["id"], ttl_hours=-2)

    assert db.purge_expired_sessions() == 2
    assert db.user_for_token(live) is not None


def test_change_password(client):
    tok = signup(client, "Alice", "pw")["token"]
    r = client.post(
        "/api/change-password",
        json={"current_password": "pw", "new_password": "new"},
        headers=auth(tok),
    )
    assert r.status_code == 200
    assert client.post("/api/login", json={"name": "Alice", "password": "pw"}).status_code == 401
    assert signup(client, "Alice", "new")["token"]


def test_change_password_rejects_a_wrong_current_password(client):
    tok = signup(client, "Alice", "pw")["token"]
    r = client.post(
        "/api/change-password",
        json={"current_password": "wrong", "new_password": "new"},
        headers=auth(tok),
    )
    assert r.status_code == 400


# ── login throttle ────────────────────────────────────────────────────────────
def test_repeated_failures_are_throttled(client):
    from app.main import LOGIN_LIMITER

    signup(client, "Alice", "pw")
    limit = LOGIN_LIMITER.max_failures

    for _ in range(limit):
        assert client.post("/api/login", json={"name": "Alice", "password": "x"}).status_code == 401

    r = client.post("/api/login", json={"name": "Alice", "password": "x"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_throttle_blocks_even_the_correct_password(client):
    """Otherwise the limit is trivially bypassed by guessing until you land it."""
    from app.main import LOGIN_LIMITER

    signup(client, "Alice", "pw")
    for _ in range(LOGIN_LIMITER.max_failures):
        client.post("/api/login", json={"name": "Alice", "password": "x"})

    assert client.post("/api/login", json={"name": "Alice", "password": "pw"}).status_code == 429


def test_a_successful_sign_in_clears_the_count(client):
    from app.main import LOGIN_LIMITER

    signup(client, "Alice", "pw")
    for _ in range(LOGIN_LIMITER.max_failures - 1):
        client.post("/api/login", json={"name": "Alice", "password": "x"})

    assert signup(client, "Alice", "pw")["token"]  # succeeds, resetting the window
    for _ in range(LOGIN_LIMITER.max_failures - 1):
        assert client.post("/api/login", json={"name": "Alice", "password": "x"}).status_code == 401


def test_throttling_one_name_does_not_lock_out_another(client):
    from app.main import LOGIN_LIMITER

    signup(client, "Alice", "pw")
    signup(client, "Bob", "pw")
    for _ in range(LOGIN_LIMITER.max_failures + 1):
        client.post("/api/login", json={"name": "Alice", "password": "x"})

    assert signup(client, "Bob", "pw")["token"]


def test_throttled_response_exposes_retry_after_cross_origin(client):
    """Retry-After is not CORS-safelisted, so the API must expose it explicitly.

    Without the expose_headers entry the dashboard cannot read the wait on a
    cross-origin request, which is the only way it is ever deployed.
    """
    from app.main import LOGIN_LIMITER

    signup(client, "Alice", "pw")
    origin = {"Origin": "https://heartbeat.netlify.app"}
    for _ in range(LOGIN_LIMITER.max_failures):
        client.post("/api/login", json={"name": "Alice", "password": "x"}, headers=origin)

    r = client.post("/api/login", json={"name": "Alice", "password": "x"}, headers=origin)
    assert r.status_code == 429
    assert r.headers["Retry-After"]
    exposed = r.headers.get("access-control-expose-headers", "")
    assert "Retry-After" in exposed, exposed
    # The body carries the same wait, so a client that cannot read the header
    # still has something to show.
    assert "Try again in" in r.json()["detail"]
