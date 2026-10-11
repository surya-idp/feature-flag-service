"""Kubernetes ServiceAccount tokens as a second way to authenticate (app/kube_identity.py).

Two layers are tested:
  1. the API's behaviour, with the question to Kubernetes replaced by a fake answer;
  2. the question itself, against a small local HTTP server that plays the Kubernetes API,
     so the request this service really sends (path, body, audience) is checked too.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import kube_identity
from app.config import settings
from tests.conftest import bearer

PORTAL = "system:serviceaccount:backstage:idp-portal"
ORDERS = "system:serviceaccount:orders-svc:orders-svc"
NEW_FLAG = {"name": "new-checkout", "environment": "dev", "flag_type": "boolean", "default_value": True}


@pytest.fixture(autouse=True)
def kube_on(monkeypatch):
    """Feature on, Backstage's ServiceAccount is the admin, and a clean cache per test."""
    monkeypatch.setattr(settings, "kube_identity", "on")
    monkeypatch.setattr(settings, "kube_admin_serviceaccounts", frozenset({PORTAL}))
    kube_identity.clear_cache()
    yield
    kube_identity.clear_cache()


@pytest.fixture
def kubernetes(monkeypatch):
    """Fake answers: kubernetes.tokens maps a token to the username Kubernetes would return."""

    class Fake:
        tokens: dict[str, str] = {}
        asked: list[str] = []

    fake = Fake()
    fake.tokens, fake.asked = {}, []

    def review(token: str):
        fake.asked.append(token)
        return fake.tokens.get(token)

    monkeypatch.setattr(kube_identity, "_review", review)
    return fake


def sa(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- behaviour of the API ----

def test_a_service_can_read_flags_with_its_serviceaccount_token(api, kubernetes):
    kubernetes.tokens["orders-token"] = ORDERS
    assert api.get("/flags", headers=sa("orders-token")).status_code == 200


def test_a_service_can_evaluate_a_flag(api, kubernetes, admin):
    kubernetes.tokens["orders-token"] = ORDERS
    assert api.post("/flags", json=NEW_FLAG, headers=admin).status_code == 201
    r = api.post(
        "/evaluate",
        json={"flag_name": "new-checkout", "environment": "dev", "user_id": "u1"},
        headers=sa("orders-token"),
    )
    assert r.status_code == 200, r.text


def test_an_ordinary_service_cannot_create_a_flag(api, kubernetes):
    kubernetes.tokens["orders-token"] = ORDERS
    r = api.post("/flags", json=NEW_FLAG, headers=sa("orders-token"))
    assert r.status_code == 403            # known caller, not allowed: 403, not 401


def test_the_listed_admin_serviceaccount_can_create_a_flag(api, kubernetes):
    kubernetes.tokens["portal-token"] = PORTAL
    r = api.post("/flags", json=NEW_FLAG, headers=sa("portal-token"))
    assert r.status_code == 201, r.text


def test_the_audit_trail_names_the_serviceaccount(api, kubernetes, db_session_factory):
    from app.models import AuditLog

    kubernetes.tokens["portal-token"] = PORTAL
    assert api.post("/flags", json=NEW_FLAG, headers=sa("portal-token")).status_code == 201
    db = db_session_factory()
    try:
        assert [row.actor for row in db.query(AuditLog).all()] == [PORTAL]
    finally:
        db.close()


def test_a_token_kubernetes_does_not_recognise_is_401(api, kubernetes):
    r = api.get("/flags", headers=sa("made-up"))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_a_real_token_of_a_human_or_node_is_refused(api, kubernetes):
    kubernetes.tokens["human-token"] = "kubernetes-admin"
    kubernetes.tokens["node-token"] = "system:node:ip-10-0-11-4"
    assert api.get("/flags", headers=sa("human-token")).status_code == 401
    assert api.get("/flags", headers=sa("node-token")).status_code == 401


def test_a_similar_name_does_not_become_admin(api, kubernetes):
    kubernetes.tokens["t"] = "system:serviceaccount:backstage:idp-portal-evil"
    assert api.post("/flags", json=NEW_FLAG, headers=sa("t")).status_code == 403


def test_login_tokens_still_work_and_kubernetes_is_not_asked(api, kubernetes, viewer):
    assert api.get("/flags", headers=viewer).status_code == 200
    assert kubernetes.asked == []


def test_switched_off_means_kubernetes_is_never_asked(api, kubernetes, monkeypatch):
    monkeypatch.setattr(settings, "kube_identity", "off")
    kubernetes.tokens["orders-token"] = ORDERS
    assert api.get("/flags", headers=sa("orders-token")).status_code == 401
    assert kubernetes.asked == []


def test_a_verified_token_is_remembered_but_a_rejected_one_is_asked_again(api, kubernetes):
    kubernetes.tokens["orders-token"] = ORDERS
    for _ in range(3):
        assert api.get("/flags", headers=sa("orders-token")).status_code == 200
    assert kubernetes.asked == ["orders-token"]

    for _ in range(2):
        assert api.get("/flags", headers=sa("bad")).status_code == 401
    assert kubernetes.asked == ["orders-token", "bad", "bad"]


# ------------------------------------------------------- the question to Kubernetes ----

@pytest.fixture
def kube_api(monkeypatch):
    """A local server that plays the Kubernetes API's TokenReview endpoint."""

    class State:
        requests: list[dict] = []
        answer: dict | str = {}
        status = 201

    state = State()
    state.requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append({"path": self.path, "body": body})
            payload = state.answer if isinstance(state.answer, str) else json.dumps(state.answer)
            self.send_response(state.status)
            self.end_headers()
            self.wfile.write(payload.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "kube_api_url", f"http://127.0.0.1:{server.server_port}")
    yield state
    server.shutdown()


def reviewed(username: str, audiences: list[str], authenticated: bool = True) -> dict:
    return {"status": {"authenticated": authenticated, "audiences": audiences,
                       "user": {"username": username}}}


def test_the_review_request_names_the_token_and_our_audience(kube_api):
    kube_api.answer = reviewed(ORDERS, ["feature-flag-api"])
    assert kube_identity.identify("orders-token") == kube_identity.KubeIdentity(ORDERS, "viewer")
    assert kube_api.requests == [{
        "path": "/apis/authentication.k8s.io/v1/tokenreviews",
        "body": {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
                 "spec": {"token": "orders-token", "audiences": ["feature-flag-api"]}},
    }]


def test_the_admin_list_decides_the_role(kube_api):
    kube_api.answer = reviewed(PORTAL, ["feature-flag-api"])
    assert kube_identity.identify("portal-token").role == "admin"


def test_not_authenticated_is_refused(kube_api):
    kube_api.answer = reviewed(ORDERS, ["feature-flag-api"], authenticated=False)
    assert kube_identity.identify("t") is None


def test_a_token_for_another_audience_is_refused(kube_api):
    # e.g. the pod's default token, valid for the Kubernetes API, replayed against us
    kube_api.answer = reviewed(ORDERS, ["https://kubernetes.default.svc"])
    assert kube_identity.identify("t") is None
    kube_api.answer = reviewed(ORDERS, [])
    assert kube_identity.identify("t") is None


@pytest.mark.parametrize("status, answer", [(403, {"message": "forbidden"}), (500, "boom"), (201, "not json")])
def test_a_broken_kubernetes_api_means_refused_not_a_crash(kube_api, status, answer):
    kube_api.status, kube_api.answer = status, answer
    assert kube_identity.identify("t") is None


def test_kubernetes_unreachable_means_refused(monkeypatch):
    monkeypatch.setattr(settings, "kube_api_url", "http://127.0.0.1:1")   # nothing listens
    assert kube_identity.identify("t") is None


def test_unreachable_kubernetes_is_a_401_at_the_api(api, monkeypatch):
    monkeypatch.setattr(settings, "kube_api_url", "http://127.0.0.1:1")
    assert api.get("/flags", headers=sa("anything")).status_code == 401
