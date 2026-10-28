
import app as app_module


def test_is_technician_returns_true_for_technician():
    user = app_module.User.__new__(app_module.User)
    user.role = "technician"

    assert user.is_technician is True


def test_is_technician_returns_false_for_admin():
    user = app_module.User.__new__(app_module.User)
    user.role = "admin"

    assert user.is_technician is False
