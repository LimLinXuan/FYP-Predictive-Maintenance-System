
import pytest
from unittest.mock import MagicMock, patch

import app as app_module


@pytest.fixture
def client():
    """Create a Flask test client without requiring login."""
    previous_value = app_module.app.config.get("LOGIN_DISABLED", False)
    app_module.app.config["LOGIN_DISABLED"] = True

    try:
        with app_module.app.test_client() as test_client:
            yield test_client
    finally:
        app_module.app.config["LOGIN_DISABLED"] = previous_value


def test_machine_detail_page_found(client):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = {
        "asset_id": "ASSET001"
    }

    with patch("app.get_db", return_value=mock_conn), \
         patch(
             "app.render_template",
             return_value="Machine detail"
         ) as mock_render:

        response = client.get("/machine/MACHINE001")

    assert response.status_code == 200

    mock_render.assert_called_once_with(
        "asset_detail.html",
        asset_id="ASSET001"
    )


def test_machine_detail_page_not_found(client):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None

    with patch("app.get_db", return_value=mock_conn), \
         patch(
             "app.render_template",
             return_value="Not found"
         ) as mock_render:

        response = client.get("/machine/UNKNOWN001")

    assert response.status_code == 404

    mock_render.assert_called_once_with(
        "asset_detail.html",
        asset_id="UNKNOWN001"
    )
