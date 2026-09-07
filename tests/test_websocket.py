"""The live socket: who may open one, and what it is allowed to carry.

The close-code tests assert 4401 specifically. Closing the socket *before*
accepting it would make a real server reject the handshake with HTTP 403, which
browsers report as 1006 - indistinguishable from a dropped connection, so the
dashboard would reconnect forever instead of signing out. TestClient speaks ASGI
and does not reproduce that difference, so these guard the contract while
tests/test_websocket_live.py covers the wire behaviour.

Frames are read through ws_receive so a socket that is wrongly left open fails
the test instead of hanging the suite.
"""

from __future__ import annotations

import json

from conftest import BEAT, auth, signup, ws_receive


def _close_code(ws):
    """TestClient hands back the close frame as a message rather than raising."""
    msg = ws_receive(ws)
    assert msg["type"] == "websocket.close", msg
    return msg["code"]


def _beat_frame(ws):
    msg = ws_receive(ws)
    assert msg["type"] == "websocket.send", msg
    return json.loads(msg["text"])


def test_socket_without_a_token_is_closed_with_4401(client):
    with client.websocket_connect("/ws") as ws:
        assert _close_code(ws) == 4401


def test_socket_with_a_bad_token_is_closed_with_4401(client):
    with client.websocket_connect("/ws?token=garbage") as ws:
        assert _close_code(ws) == 4401


def test_socket_with_an_expired_token_is_closed_with_4401(client, env):
    from app import database as db

    user = signup(client, "Alice", "pw")
    stale = db.create_session(user["id"], ttl_hours=-1)
    with client.websocket_connect(f"/ws?token={stale}") as ws:
        assert _close_code(ws) == 4401


def test_socket_receives_the_users_own_beats(client):
    alice = signup(client, "Alice", "pw")
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws:
        client.post("/api/beats", json=BEAT, headers=auth(alice["token"]))
        msg = _beat_frame(ws)
    assert msg["type"] == "beat"
    assert msg["data"]["user_id"] == alice["id"]


def test_a_beat_never_reaches_another_users_socket(client):
    """The bug this guards: a flat broadcast list streamed every ECG to everyone."""
    alice = signup(client, "Alice", "pw")
    bob = signup(client, "Bob", "pw")

    with client.websocket_connect(f"/ws?token={bob['token']}") as bob_ws:
        client.post("/api/beats", json=BEAT, headers=auth(alice["token"]))
        client.post("/api/beats", json=dict(BEAT, class_code="V"), headers=auth(bob["token"]))
        msg = _beat_frame(bob_ws)

    # If Alice's beat leaked, it would arrive first.
    assert msg["data"]["user_id"] == bob["id"]
    assert msg["data"]["class_code"] == "V"
