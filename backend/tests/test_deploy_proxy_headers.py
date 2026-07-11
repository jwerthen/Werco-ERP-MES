"""
Regression guard for the reverse-proxy launch config.

Behind Railway's TLS-terminating proxy, uvicorn/gunicorn must be told to trust the
proxy's ``X-Forwarded-Proto`` header. ``--proxy-headers`` alone is not enough: uvicorn
only honors the forwarded scheme when the peer IP is in ``forwarded_allow_ips`` (default
``127.0.0.1``), and the proxy is not localhost. Without the trust setting,
``request.url.scheme`` stays ``http``, so FastAPI's automatic trailing-slash 307 redirect
builds an ``http://`` ``Location`` that a browser on an ``https`` page refuses to follow —
the request fails with status 0 (this is what blanked the OEE dashboard when it called the
slashless ``/work-centers`` path).

These tests assert every launch path keeps the proxy-header trust so the fix can't silently
regress. They only read the deploy files, so they need no DB or network.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (BACKEND_ROOT / rel).read_text()


def test_dockerfile_uvicorn_trusts_forwarded_headers():
    """The Railway build (railway.toml -> Dockerfile) runs uvicorn; it must trust the proxy."""
    text = _read("Dockerfile")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips=*" in text


def test_dockerfile_sets_forwarded_allow_ips_env():
    """Railway can override the container start command, so the CMD's --forwarded-allow-ips=*
    may never apply. uvicorn also reads FORWARDED_ALLOW_IPS from the environment regardless of
    how it's launched, so the Dockerfile must set it as env-level defense-in-depth (this is why
    prod still 307-redirected to an http:// Location even after the CMD flag was added)."""
    text = _read("Dockerfile")
    assert "FORWARDED_ALLOW_IPS=*" in text


def test_railway_builds_from_the_hardened_dockerfile():
    """Guard the assumption these tests rely on: railway.toml builds from ``Dockerfile``."""
    text = _read("railway.toml")
    assert 'dockerfilePath = "Dockerfile"' in text


def test_nixpacks_uvicorn_trusts_forwarded_headers():
    text = _read("nixpacks.toml")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips=*" in text


def test_start_script_uvicorn_trusts_forwarded_headers():
    text = _read("start.sh")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips=*" in text


def test_prod_dockerfile_sets_forwarded_allow_ips():
    """Dockerfile.prod uses gunicorn's UvicornWorker, which reads FORWARDED_ALLOW_IPS from env."""
    text = _read("Dockerfile.prod")
    assert "FORWARDED_ALLOW_IPS=*" in text
