
import app as app_module


def test_cached_response_returns_none_when_key_is_missing():
    key = "test_missing_cache_key"
    app_module._response_cache.pop(key, None)

    assert app_module._cached_response(key) is None


def test_store_cached_response_can_be_retrieved():
    key = "test_store_cache_key"
    value = {"message": "cached"}
    app_module._response_cache.pop(key, None)

    try:
        result = app_module._store_cached_response(key, value)

        assert result == value
        assert app_module._cached_response(key) == value
    finally:
        app_module._response_cache.pop(key, None)


def test_cached_response_returns_none_when_expired(monkeypatch):
    key = "test_expired_cache_key"
    app_module._response_cache.pop(key, None)

    monkeypatch.setattr(app_module, "_RESPONSE_CACHE_TTL", 10)
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 100)

    app_module._response_cache[key] = (80, {"message": "old"})

    try:
        assert app_module._cached_response(key) is None
        assert key not in app_module._response_cache
    finally:
        app_module._response_cache.pop(key, None)


def test_cached_response_returns_value_when_not_expired(monkeypatch):
    key = "test_valid_cache_key"
    value = {"message": "still valid"}
    app_module._response_cache.pop(key, None)

    monkeypatch.setattr(app_module, "_RESPONSE_CACHE_TTL", 10)
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 105)

    app_module._response_cache[key] = (100, value)

    try:
        assert app_module._cached_response(key) == value
    finally:
        app_module._response_cache.pop(key, None)
