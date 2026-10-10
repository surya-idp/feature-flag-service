"""Authentication and authorisation: login, tokens, and who may do what.

These are the rules a security review would ask about, each as a test:
  - a login only succeeds with the right password, and failure does not reveal which part
    was wrong (no username enumeration)
  - passwords are stored hashed
  - a request without a valid token gets 401 (not authenticated)
  - a valid token with the wrong role gets 403 (authenticated, not allowed)
  - a tampered or expired token is rejected
"""
from datetime import datetime, timedelta, timezone

from jose import jwt

from app.auth import hash_password, verify_password
from app.config import settings
from tests.conftest import bearer

FLAG = {"name": "auth-test-flag", "flag_type": "boolean", "default_value": True, "environment": "dev"}


# ---- login ------------------------------------------------------------------------------

def test_login_returns_a_bearer_token_for_valid_credentials(api, make_user):
    make_user("alice", "correct horse", "admin")
    response = api.post("/auth/login", data={"username": "alice", "password": "correct horse"})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"].count(".") == 2          # header.payload.signature


def test_token_from_login_is_accepted_and_carries_the_role(api, make_user):
    make_user("alice", "correct horse", "admin")
    token = api.post("/auth/login", data={"username": "alice", "password": "correct horse"}).json()["access_token"]
    created = api.post("/flags", json=FLAG, headers={"Authorization": f"Bearer {token}"})
    assert created.status_code == 201                    # admin-only route: the role came through


def test_wrong_password_and_unknown_user_look_the_same(api, make_user):
    make_user("alice", "correct horse")
    wrong_password = api.post("/auth/login", data={"username": "alice", "password": "nope"})
    unknown_user = api.post("/auth/login", data={"username": "nobody", "password": "nope"})
    assert wrong_password.status_code == unknown_user.status_code == 401
    # Same message: an attacker must not learn which usernames exist.
    assert wrong_password.json() == unknown_user.json()
    assert wrong_password.headers["www-authenticate"] == "Bearer"


def test_login_needs_both_fields(api):
    assert api.post("/auth/login", data={"username": "alice"}).status_code == 422


def test_passwords_are_stored_hashed_not_in_plaintext():
    hashed = hash_password("correct horse")
    assert hashed != "correct horse"
    assert hashed.startswith("$2")                       # bcrypt
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong", hashed)
    assert hash_password("correct horse") != hashed      # salted: same input, different hash


# ---- 401: not authenticated -------------------------------------------------------------

def test_no_token_is_401(api):
    response = api.get("/flags")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_garbage_token_is_401(api):
    assert api.get("/flags", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_token_signed_with_another_key_is_401(api):
    forged = jwt.encode(
        {"sub": "mallory", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "some-other-secret",
        algorithm=settings.jwt_algorithm,
    )
    assert api.get("/flags", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_expired_token_is_401(api):
    expired = jwt.encode(
        {"sub": "alice", "role": "admin", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert api.get("/flags", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_token_without_a_role_is_401(api):
    no_role = jwt.encode(
        {"sub": "alice", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert api.get("/flags", headers={"Authorization": f"Bearer {no_role}"}).status_code == 401


# ---- 403: authenticated, not allowed ----------------------------------------------------

def test_viewer_can_read(api, viewer):
    assert api.get("/flags", headers=viewer).status_code == 200


def test_viewer_cannot_create_update_or_delete(api, admin, viewer):
    flag_id = api.post("/flags", json=FLAG, headers=admin).json()["id"]
    assert api.post("/flags", json={**FLAG, "name": "another"}, headers=viewer).status_code == 403
    assert api.patch(f"/flags/{flag_id}", json={"description": "x"}, headers=viewer).status_code == 403
    assert api.delete(f"/flags/{flag_id}", headers=viewer).status_code == 403
    # ... and nothing changed
    assert api.get(f"/flags/{flag_id}", headers=viewer).json()["description"] is None


def test_an_unknown_role_is_not_admin(api):
    assert api.post("/flags", json=FLAG, headers=bearer("eve", "superuser")).status_code == 403
