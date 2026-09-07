"""Who may post a beat, and whose account it lands on."""

from __future__ import annotations

from conftest import BEAT, auth, signup


def test_ingest_without_credentials_is_refused(client):
    assert client.post("/api/beats", json=BEAT).status_code == 401


def test_ingest_with_a_wrong_device_key_is_refused(client):
    r = client.post("/api/beats", json=BEAT, headers={"X-Device-Key": "wrong"})
    assert r.status_code == 401


def test_device_key_may_post(client, device_headers):
    r = client.post("/api/beats", json=BEAT, headers=device_headers)
    assert r.status_code == 200
    assert r.json()["classification"] == "Normal"


def test_device_key_may_name_an_existing_account(client, device_headers):
    alice = signup(client, "Alice", "pw")
    r = client.post("/api/beats", json=dict(BEAT, user_id=alice["id"]), headers=device_headers)
    assert r.status_code == 200
    assert r.json()["user_id"] == alice["id"]
    assert r.json()["patient"] == "Alice"


def test_unknown_user_id_is_rejected_rather_than_stored(client, device_headers):
    r = client.post("/api/beats", json=dict(BEAT, user_id=99999), headers=device_headers)
    assert r.status_code == 404


def test_a_signed_in_user_cannot_post_into_someone_elses_history(client):
    """The payload's patient/user_id must not override the token's owner."""
    alice = signup(client, "Alice", "pw")
    bob = signup(client, "Bob", "pw")

    r = client.post(
        "/api/beats",
        json=dict(BEAT, user_id=bob["id"], patient="Bob"),
        headers=auth(alice["token"]),
    )
    assert r.status_code == 200
    assert r.json()["user_id"] == alice["id"]
    assert r.json()["patient"] == "Alice"

    assert client.get("/api/history", headers=auth(bob["token"])).json() == []


def test_history_is_scoped_to_the_signed_in_user(client):
    alice = signup(client, "Alice", "pw")
    bob = signup(client, "Bob", "pw")
    client.post("/api/beats", json=BEAT, headers=auth(alice["token"]))
    client.post("/api/beats", json=dict(BEAT, class_code="V"), headers=auth(bob["token"]))

    a = client.get("/api/history", headers=auth(alice["token"])).json()
    b = client.get("/api/history", headers=auth(bob["token"])).json()
    assert [x["class_code"] for x in a] == ["N"]
    assert [x["class_code"] for x in b] == ["V"]


def test_alert_level_is_computed_server_side(client, device_headers):
    """A confident ventricular beat is the documented red case."""
    r = client.post(
        "/api/beats",
        json={"class_code": "V", "confidence": 0.95, "bpm": 80},
        headers=device_headers,
    )
    assert r.json()["alert_level"] == "red"
    assert r.json()["is_abnormal"] is True


def test_low_confidence_reads_as_uncertain(client, device_headers):
    r = client.post(
        "/api/beats", json={"class_code": "N", "confidence": 0.2}, headers=device_headers
    )
    assert r.json()["alert_level"] == "uncertain"
