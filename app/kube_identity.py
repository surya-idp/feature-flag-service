"""
kube_identity.py - accept a Kubernetes ServiceAccount token as proof of who the caller is.

Why
---
Other services on the platform must call POST /evaluate, and Backstage must create flags.
Until now the only way to get a token was POST /auth/login with a username and password, so
every calling service would have needed a password of its own, stored and rotated somewhere.

Kubernetes already gives every pod an identity: its ServiceAccount. A pod can ask for a
short-lived token that is valid for ONE named audience (a "projected" token). The caller sends
that token; this module asks the Kubernetes API "is this token real, was it issued for me, and
whose is it?" (the TokenReview API). No secret is created, stored or rotated by anyone.

Who the caller is afterwards
----------------------------
  username  "system:serviceaccount:<namespace>:<name>"   (what Kubernetes returns)
  role      "admin" if that name is listed in KUBE_ADMIN_SERVICEACCOUNTS, else "viewer"

A viewer may read and evaluate flags; only an admin may change them (dependencies.py).

Why the audience matters
------------------------
Every pod also has a default token that is valid for the Kubernetes API itself. If this
service accepted that one, any service it calls could replay the token against the cluster.
A token issued for the audience "feature-flag-api" is useless anywhere else, and this service
refuses a token that was not issued for it.

Failure is never an exception
-----------------------------
`identify()` returns None for "not a valid ServiceAccount token" and for "could not ask
Kubernetes". The caller turns None into HTTP 401. A broken cluster API must not become a 500.
"""

import hashlib
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# Where Kubernetes mounts a pod's own credentials. This service uses its OWN token to be
# allowed to ask the question (it needs the cluster role system:auth-delegator).
_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_SERVICEACCOUNT_PREFIX = "system:serviceaccount:"

# A verified token is remembered for a short time, so a service that evaluates flags many
# times a second does not cause one TokenReview per request. Keyed by a hash: the token
# itself is never kept. Only successes are cached; a rejected token is asked about again.
_CACHE_SECONDS = 60
_cache: dict[str, tuple[float, "KubeIdentity"]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class KubeIdentity:
    username: str   # system:serviceaccount:<namespace>:<name>
    role: str       # "admin" or "viewer"


def enabled() -> bool:
    """On when configured on, or (by default) when running inside a Kubernetes pod."""
    if settings.kube_identity == "on":
        return True
    if settings.kube_identity == "off":
        return False
    return (_SA_DIR / "token").exists()


def identify(token: str) -> KubeIdentity | None:
    """Return the caller's identity if `token` is a valid ServiceAccount token for us."""
    if not enabled():
        return None

    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

    username = _review(token)
    if username is None:
        return None
    if not username.startswith(_SERVICEACCOUNT_PREFIX):
        # A human user's or a node's token: real, but not what this door is for.
        logger.warning("kube identity refused: %s is not a ServiceAccount", username)
        return None

    role = "admin" if username in settings.kube_admin_serviceaccounts else "viewer"
    identity = KubeIdentity(username=username, role=role)
    with _cache_lock:
        if len(_cache) > 1000:          # bounded: tokens rotate about hourly
            _cache.clear()
        _cache[key] = (now + _CACHE_SECONDS, identity)
    return identity


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _review(token: str) -> str | None:
    """Ask Kubernetes about `token`. Returns the username, or None if it is not valid for us."""
    audience = settings.kube_token_audience
    body = json.dumps({
        "apiVersion": "authentication.k8s.io/v1",
        "kind": "TokenReview",
        "spec": {"token": token, "audiences": [audience]},
    }).encode()
    url = settings.kube_api_url.rstrip("/") + "/apis/authentication.k8s.io/v1/tokenreviews"
    try:
        headers = {"Content-Type": "application/json"}
        own_token = _SA_DIR / "token"
        if own_token.exists():
            # Read on every call: Kubernetes replaces the file before the token expires.
            headers["Authorization"] = f"Bearer {own_token.read_text().strip()}"
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        context = None
        if url.startswith("https://"):
            ca = _SA_DIR / "ca.crt"
            context = ssl.create_default_context(cafile=str(ca) if ca.exists() else None)
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            status = json.load(response).get("status") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Includes HTTP errors (403 = this service lacks system:auth-delegator), timeouts and
        # a body that is not JSON. Fail closed.
        logger.error("kube identity: TokenReview failed: %s", exc)
        return None

    if not status.get("authenticated"):
        return None
    # Kubernetes lists the audiences the token is valid for among those we asked about. An
    # API server that ignores `spec.audiences` returns none: treat that as "not for us".
    if audience not in (status.get("audiences") or []):
        logger.warning("kube identity refused: token not issued for audience %s", audience)
        return None
    return (status.get("user") or {}).get("username") or None
