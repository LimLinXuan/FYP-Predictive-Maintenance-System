import pytest
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True

    with app.test_client() as test_client:
        yield test_client


def login_as_user(client):
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True


PAGE_ROUTES = [
    "/factory",
    "/alerts",
    "/metrics",
    "/model-comparison",
    "/explainability",
    "/health",
    "/record/100",
    "/records",
    "/production-lines",
    "/production-lines/test-line",
    "/assets",
    "/assets/ASSET-001",
    "/stream",
    "/notifications",
]


@pytest.mark.parametrize("path", PAGE_ROUTES)
def test_page_route_requires_login(client, path):
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 302
    assert "/login" in response.headers.get("Location", "").lower()


@pytest.mark.parametrize("path", PAGE_ROUTES)
def test_page_route_authenticated(client, path):
    login_as_user(client)

    response = client.get(path)

    assert response.status_code == 200
    assert b"<html" in response.data.lower()
