"""Test setup. No test may reach the network or use real keys.

The dummy keys are set BEFORE the app is imported: importing `app` loads ./.env, but it never replaces a variable
that is already set, so the real keys in .env are never read by the tests."""
import os
import socket

for _k, _v in {
    "PAYPAL_CLIENT_ID": "test-client-id",
    "PAYPAL_CLIENT_SECRET": "test-client-secret",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "ANTHROPIC_MODEL": "test-model",
    "PAYPAL_BASE": "https://api-m.sandbox.paypal.com",
    "SAFE_MODE": "0",
    "PAYPAL_WEBHOOK_ID": "placeholder-removed-by-the-fixture-below",  # stops a real .env value being loaded
}.items():
    os.environ[_k] = _v

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ai, live, main, paypal  # noqa: E402
from helpers import FakePayPal  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    """Any attempt to open a network connection fails the test."""
    def blocked(*a, **kw):
        raise RuntimeError("The tests must not use the network")
    mp = pytest.MonkeyPatch()
    mp.setattr(socket.socket, "connect", blocked)
    mp.setattr(socket, "create_connection", blocked)
    mp.setattr(socket, "getaddrinfo", blocked)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    monkeypatch.delenv("SAFE_MODE", raising=False)
    monkeypatch.setattr(paypal, "BASE", "https://api-m.sandbox.paypal.com")
    live.reset()
    paypal._results.clear()
    paypal._attempts.clear()
    paypal.reset_token()
    ai.reset_client()
    ai.reset_nudge_cache()
    main._demo["result"] = None
    yield


@pytest.fixture
def fake_pp(monkeypatch):
    """Replaces paypal._call. Register answers with fake_pp.route(method, path, response)."""
    fake = FakePayPal()
    monkeypatch.setattr(paypal, "_call", fake)
    return fake


@pytest.fixture
def no_pp(monkeypatch):
    """PayPal must not be called at all."""
    fake = FakePayPal()
    monkeypatch.setattr(paypal, "_call", fake)
    return fake


@pytest.fixture
def client():
    """A browser on this computer: Host is localhost and the connection comes from 127.0.0.1."""
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 50000),
                      raise_server_exceptions=False)


@pytest.fixture
def remote_client():
    """Right Host header, but the connection comes from another machine."""
    return TestClient(main.app, base_url="http://localhost", client=("203.0.113.9", 50000),
                      raise_server_exceptions=False)
