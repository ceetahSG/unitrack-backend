"""CORS is off unless configured, and never wildcard.

`unitrack-web` cannot call this API from a browser without CORS, so the setting
exists. But CORS on an authenticated API is the one middleware where a lazy
default is genuinely dangerous: `allow_origins=["*"]` lets any page on the
internet script requests against it.

These tests pin both halves — that it stays absent until asked for, and that it
cannot be turned into a wildcard by an env var.
"""

from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings
from app.main import create_app


def _cors_layer(app):
    return next(
        (m for m in app.user_middleware if m.cls is CORSMiddleware),
        None,
    )


def test_no_cors_middleware_when_unset(monkeypatch) -> None:
    """The default posture. A server with no web client advertises nothing."""
    monkeypatch.setattr("app.main.settings", Settings(cors_origins=""))
    assert _cors_layer(create_app()) is None


def test_cors_middleware_added_when_origins_given(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.settings", Settings(cors_origins="http://localhost:3000")
    )
    layer = _cors_layer(create_app())
    assert layer is not None
    assert layer.kwargs["allow_origins"] == ["http://localhost:3000"]


def test_credentials_stay_off(monkeypatch) -> None:
    """Bearer tokens travel in a header, so the browser has no credentials to
    attach. Leaving this off keeps the blast radius of a misconfigured origin
    small."""
    monkeypatch.setattr(
        "app.main.settings", Settings(cors_origins="http://localhost:3000")
    )
    assert _cors_layer(create_app()).kwargs["allow_credentials"] is False


def test_a_wildcard_origin_is_dropped() -> None:
    """`*` in the env var must not become `allow_origins=["*"]`."""
    assert Settings(cors_origins="*").cors_origin_list == []


def test_a_wildcard_among_real_origins_is_dropped_too() -> None:
    """The dangerous entry goes; the legitimate ones survive."""
    parsed = Settings(cors_origins="http://localhost:3000, *").cors_origin_list
    assert parsed == ["http://localhost:3000"]


def test_whitespace_and_blanks_are_ignored() -> None:
    """Trailing commas in a .env file are normal and must not yield an origin
    of `""`, which would never match and would silently break every request."""
    assert Settings(cors_origins=" http://a.test , ,http://b.test ").cors_origin_list == [
        "http://a.test",
        "http://b.test",
    ]
