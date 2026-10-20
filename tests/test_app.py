import pytest
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield client


def test_home_page(client):
    response = client.get("/")

    assert response.status_code in (200, 302)


def test_stream_page_requires_login(client):
    """Unauthenticated users should not access the stream page."""
    response = client.get("/stream", follow_redirects=False)

    assert response.status_code == 302
    assert "/login" in response.headers.get("Location", "").lower()


def test_stream_page_authenticated(client):
    """Authenticated users should be able to access the stream page."""
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True

    response = client.get("/stream")

    assert response.status_code == 200
