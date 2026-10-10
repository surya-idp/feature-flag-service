"""Shared fixtures for the API tests (test_auth.py, test_flags_api.py).

Each test gets its own empty SQLite database and its own dependency overrides, and both are
put back afterwards. That matters here: the older test modules replace dependencies on the
shared `app` object at import time (for example, test_metrics.py makes every request a
"viewer"), so a test that wants the REAL authentication must set that up itself and must not
leak its own setup either.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import create_access_token, hash_password
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.main import app
from app.models import User


@pytest.fixture
def db_session_factory():
    """An empty in-memory database with the full schema, shared by all connections."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,          # one connection: every session sees the same data
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def api(db_session_factory):
    """A client for the real app: real JWT authentication, the test database."""

    def _get_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _get_db
    # Whatever another module installed: these tests authenticate for real.
    app.dependency_overrides.pop(get_current_user, None)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


@pytest.fixture
def make_user(db_session_factory):
    """make_user("alice", "s3cret", "admin") stores a user with a hashed password."""

    def _make(username: str, password: str, role: str = "viewer") -> None:
        db = db_session_factory()
        db.add(User(username=username, hashed_password=hash_password(password), role=role))
        db.commit()
        db.close()

    return _make


def bearer(username: str, role: str) -> dict[str, str]:
    """An Authorization header with a token signed like the login endpoint signs it."""
    return {"Authorization": f"Bearer {create_access_token(username=username, role=role)}"}


@pytest.fixture
def admin() -> dict[str, str]:
    return bearer("admin-user", "admin")


@pytest.fixture
def viewer() -> dict[str, str]:
    return bearer("viewer-user", "viewer")
