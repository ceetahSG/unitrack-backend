"""Settings that are fine on a laptop and broken on a server.

None of these stop the API from booting, which is what makes them dangerous: a
misconfigured deployment answers /health perfectly and looks completely well.
`PUBLIC_BASE_URL` left at its default is not hypothetical — it cost a real
payment, because no `ipn_url` was registered and the gateway had no way to tell
us the money had moved.

So these are logged as errors at startup, and pinned here so the warnings
cannot quietly stop firing.
"""

import logging

import pytest

from app.core.config import Settings


@pytest.fixture
def warn(monkeypatch, caplog):
    """Run the startup check under a given configuration and return the log."""

    def _run(**overrides):
        import app.main as main

        settings = Settings(**overrides)
        monkeypatch.setattr(main, "settings", settings)
        monkeypatch.setattr(main, "_is_prod", settings.env == "prod")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="unitrack.startup"):
            main._warn_about_local_defaults()
        return caplog.text

    return _run


GOOD = {
    "env": "prod",
    "public_base_url": "https://api.example.edu",
    "jwt_secret": "x" * 48,
    "sslcommerz_store_id": "store",
    "sslcommerz_store_password": "secret",
}


def test_a_correctly_configured_deployment_says_nothing() -> None:
    """Noise on a healthy boot trains people to ignore the log."""
    import app.main as main

    assert main._warn_about_local_defaults() is None


def test_localhost_public_base_url_is_called_out(warn) -> None:
    """The one that actually lost a payment."""
    text = warn(**{**GOOD, "public_base_url": "http://localhost:8000"})
    assert "PUBLIC_BASE_URL" in text
    assert "ipn_url" in text


def test_a_loopback_ip_is_caught_too(warn) -> None:
    """Spelling it as an IP is the same mistake."""
    assert "PUBLIC_BASE_URL" in warn(**{**GOOD, "public_base_url": "http://127.0.0.1:8000"})


def test_the_default_jwt_secret_is_called_out(warn) -> None:
    """Anyone who reads .env.example could forge an admin token."""
    assert "JWT_SECRET" in warn(**{**GOOD, "jwt_secret": "change-me-in-prod"})


def test_a_short_jwt_secret_is_called_out(warn) -> None:
    """Under 32 bytes is below the RFC 7518 floor for HS256."""
    assert "JWT_SECRET" in warn(**{**GOOD, "jwt_secret": "short"})


def test_missing_payment_credentials_are_called_out(warn) -> None:
    assert "SSLCommerz" in warn(**{**GOOD, "sslcommerz_store_id": ""})


def test_a_healthy_production_config_is_silent(warn) -> None:
    assert warn(**GOOD).strip() == ""


def test_development_is_never_nagged(warn) -> None:
    """Localhost and a weak secret are correct in dev; warning there would make
    the production warning invisible by habit."""
    text = warn(
        env="dev",
        public_base_url="http://localhost:8000",
        jwt_secret="change-me-in-prod",
        sslcommerz_store_id="",
    )
    assert text.strip() == ""
