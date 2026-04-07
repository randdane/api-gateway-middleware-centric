"""Unit tests for VendorRegistry — DB session is mocked."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.vendors.adapters.api_key import ApiKeyAdapter
from gateway.vendors.registry import VendorConfig, VendorRegistry
from tests.unit.test_adapters import DictSecretsProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vendor_row(**overrides) -> MagicMock:
    defaults = dict(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme",
        base_url="https://api.acme.com",
        auth_type="api_key",
        auth_config={"header_name": "X-Key", "key_reference": "ACME_KEY"},
        cache_ttl_seconds=60,
        rate_limit_rpm=100,
        is_active=True,
    )
    defaults.update(overrides)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _make_session(vendors: list) -> AsyncMock:
    """Return a mock AsyncSession whose execute() returns the given vendor rows."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = vendors

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_populates_vendors():
    row = _make_vendor_row()
    session = _make_session([row])
    registry = VendorRegistry()

    await registry.load(session)

    assert "acme" in registry._vendors
    cfg = registry._vendors["acme"]
    assert cfg.slug == "acme"
    assert cfg.auth_type == "api_key"


@pytest.mark.asyncio
async def test_load_replaces_existing_entries():
    row1 = _make_vendor_row(slug="acme", name="Acme v1")
    session1 = _make_session([row1])
    registry = VendorRegistry()
    await registry.load(session1)

    row2 = _make_vendor_row(slug="acme", name="Acme v2")
    session2 = _make_session([row2])
    await registry.load(session2)

    assert registry._vendors["acme"].name == "Acme v2"


@pytest.mark.asyncio
async def test_get_returns_config():
    row = _make_vendor_row(slug="widgets")
    session = _make_session([row])
    registry = VendorRegistry()
    await registry.load(session)

    cfg = registry.get("widgets")
    assert isinstance(cfg, VendorConfig)
    assert cfg.slug == "widgets"


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown():
    registry = VendorRegistry()
    assert registry.get("nonexistent") is None


@pytest.mark.asyncio
async def test_get_adapter_builds_adapter():
    secrets = DictSecretsProvider({"ACME_KEY": "secret"})
    row = _make_vendor_row()
    session = _make_session([row])
    registry = VendorRegistry(secrets=secrets)
    await registry.load(session)

    adapter = registry.get_adapter("acme")
    assert isinstance(adapter, ApiKeyAdapter)


@pytest.mark.asyncio
async def test_get_adapter_cached():
    secrets = DictSecretsProvider({"ACME_KEY": "secret"})
    row = _make_vendor_row()
    session = _make_session([row])
    registry = VendorRegistry(secrets=secrets)
    await registry.load(session)

    a1 = registry.get_adapter("acme")
    a2 = registry.get_adapter("acme")
    assert a1 is a2


@pytest.mark.asyncio
async def test_get_adapter_returns_none_for_unknown():
    registry = VendorRegistry()
    assert registry.get_adapter("ghost") is None


@pytest.mark.asyncio
async def test_invalidate_clears_adapter_cache():
    secrets = DictSecretsProvider({"ACME_KEY": "s"})
    row = _make_vendor_row()
    session = _make_session([row])
    registry = VendorRegistry(secrets=secrets)
    await registry.load(session)

    a1 = registry.get_adapter("acme")
    registry.invalidate("acme")
    a2 = registry.get_adapter("acme")
    assert a1 is not a2


@pytest.mark.asyncio
async def test_invalidate_all_clears_everything():
    secrets = DictSecretsProvider({"ACME_KEY": "s"})
    row = _make_vendor_row()
    session = _make_session([row])
    registry = VendorRegistry(secrets=secrets)
    await registry.load(session)

    registry.get_adapter("acme")
    registry.invalidate()
    assert registry._adapters == {}


@pytest.mark.asyncio
async def test_reload_if_stale_triggers_load_when_stale():
    row = _make_vendor_row()
    session = _make_session([row])
    registry = VendorRegistry()

    # Not yet loaded → stale → should load
    await registry.reload_if_stale(session)
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_reload_if_stale_skips_when_fresh():
    row = _make_vendor_row()
    session = _make_session([row])
    registry = VendorRegistry(refresh_interval=60)

    await registry.load(session)
    initial_calls = session.execute.call_count

    await registry.reload_if_stale(session)
    assert session.execute.call_count == initial_calls


@pytest.mark.asyncio
async def test_all_vendors_returns_list():
    rows = [_make_vendor_row(slug="a"), _make_vendor_row(slug="b")]
    session = _make_session(rows)
    registry = VendorRegistry()
    await registry.load(session)

    vendors = registry.all_vendors()
    assert len(vendors) == 2
    slugs = {v.slug for v in vendors}
    assert slugs == {"a", "b"}
