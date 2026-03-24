# ruff: noqa: I001
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

if 'structlog' not in sys.modules:
    fake_structlog = types.ModuleType('structlog')

    class _FakeLogger:
        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None

            return _noop

    fake_structlog.get_logger = lambda *args, **kwargs: _FakeLogger()
    sys.modules['structlog'] = fake_structlog

from app.database.models import ProxyProduct, ProxyProductSourceMode
from app.external.proxysoxy_api import ProxySoxyCategory, ProxySoxyOrderResult, ProxySoxyProxyItem
from app.services.proxy_sales_service import (
    ProxyInsufficientBalanceError,
    ProxyQuantityError,
    proxy_sales_service,
)


class _FakeDb:
    def __init__(self) -> None:
        self.commit_calls = 0

    async def commit(self) -> None:
        self.commit_calls += 1


@pytest.mark.asyncio
async def test_fixed_markup_price_calculation() -> None:
    product = ProxyProduct(markup_type='fixed', markup_value=2500)
    assert product.calculate_sale_price_kopeks(10000) == 12500


@pytest.mark.asyncio
async def test_percent_markup_price_calculation() -> None:
    product = ProxyProduct(markup_type='percent', markup_value=15)
    assert product.calculate_sale_price_kopeks(10000) == 11500


def test_validate_quantity_accepts_any_value_in_range() -> None:
    product = ProxyProduct(min_quantity=2, max_quantity=7)
    proxy_sales_service.validate_quantity(product, 5)


def test_validate_quantity_rejects_out_of_range_value() -> None:
    product = ProxyProduct(min_quantity=2, max_quantity=7)
    with pytest.raises(ProxyQuantityError):
        proxy_sales_service.validate_quantity(product, 8)


@pytest.mark.asyncio
async def test_purchase_product_uses_stock_without_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb()
    user = SimpleNamespace(id=1, telegram_id=111, balance_kopeks=50000)
    product = ProxyProduct(
        id=7,
        name='SOCKS',
        provider_category_id='abc',
        source_mode=ProxyProductSourceMode.STOCK_FIRST.value,
        markup_type='fixed',
        markup_value=2000,
        min_quantity=1,
        max_quantity=5,
        is_active=True,
    )
    stock_item = SimpleNamespace(id=10, unit_cost_kopeks=9000, get_delivery_line=lambda: '1.1.1.1:1080')
    order = SimpleNamespace(id=22, quantity=1, total_price_kopeks=11000, user_id=user.id, status='pending')
    transaction = SimpleNamespace(id=33)

    calls = {'provider': 0, 'attach': 0}

    class _FakeClient:
        async def get_category(self, category_id: str) -> ProxySoxyCategory:
            assert category_id == 'abc'
            return ProxySoxyCategory(id='abc', name='RU', unit_cost_kopeks=9000)

    monkeypatch.setattr(proxy_sales_service, 'get_client', lambda: _FakeClient())
    monkeypatch.setattr('app.services.proxy_sales_service.get_setting_value', _async_return(None))
    monkeypatch.setattr(
        'app.services.proxy_sales_service.create_proxy_order',
        _async_return(order),
    )
    monkeypatch.setattr(
        'app.services.proxy_sales_service.reserve_proxy_stock_items',
        _async_return([stock_item]),
    )
    monkeypatch.setattr(
        'app.services.proxy_sales_service.subtract_user_balance',
        _async_return(True),
    )
    monkeypatch.setattr(
        'app.services.proxy_sales_service.create_transaction',
        _async_return(transaction),
    )
    monkeypatch.setattr('app.services.proxy_sales_service.mark_proxy_order_paid', _async_passthrough())
    monkeypatch.setattr('app.services.proxy_sales_service.mark_proxy_order_fulfilled', _async_passthrough())
    monkeypatch.setattr('app.services.proxy_sales_service.emit_transaction_side_effects', _async_return(None))

    async def _attach(*args, **kwargs):
        calls['attach'] += 1

    async def _provider_purchase(*args, **kwargs):
        calls['provider'] += 1

    monkeypatch.setattr('app.services.proxy_sales_service.attach_stock_item_to_order', _attach)
    monkeypatch.setattr('app.services.proxy_sales_service.create_proxy_provider_purchase', _provider_purchase)

    result = await proxy_sales_service.purchase_product(db, user=user, product=product, quantity=1, bot=None)

    assert result.order.id == 22
    assert result.transaction_id == 33
    assert result.items == [stock_item]
    assert calls['provider'] == 0
    assert calls['attach'] == 1
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_purchase_product_autobuy_when_stock_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb()
    user = SimpleNamespace(id=1, telegram_id=111, balance_kopeks=50000)
    product = ProxyProduct(
        id=7,
        name='SOCKS',
        provider_category_id='abc',
        source_mode=ProxyProductSourceMode.STOCK_FIRST.value,
        markup_type='fixed',
        markup_value=2000,
        min_quantity=1,
        max_quantity=5,
        is_active=True,
    )
    order = SimpleNamespace(id=22, quantity=1, total_price_kopeks=11000, user_id=user.id, status='pending', provider_purchase_id=None)
    transaction = SimpleNamespace(id=33)
    purchase = SimpleNamespace(id=44, total_cost_kopeks=9000)
    created_item = SimpleNamespace(id=55, unit_cost_kopeks=9000, get_delivery_line=lambda: '2.2.2.2:1080')
    provider_result = ProxySoxyOrderResult(
        order_id='psx-1',
        quantity=1,
        unit_cost_kopeks=9000,
        total_cost_kopeks=9000,
        currency='RUB',
        items=[
            ProxySoxyProxyItem(
                endpoint='2.2.2.2:1080',
                host='2.2.2.2',
                port=1080,
                username=None,
                password=None,
                protocol='socks5',
                country='RU',
                provider_item_id='item-1',
                expires_at=None,
                raw={},
            )
        ],
    )

    class _FakeClient:
        async def get_category(self, category_id: str) -> ProxySoxyCategory:
            return ProxySoxyCategory(id='abc', name='RU', unit_cost_kopeks=9000)

        async def create_order(self, *, category_id: str, quantity: int) -> ProxySoxyOrderResult:
            return provider_result

    monkeypatch.setattr(proxy_sales_service, 'get_client', lambda: _FakeClient())
    monkeypatch.setattr('app.services.proxy_sales_service.get_setting_value', _async_return(None))
    monkeypatch.setattr('app.services.proxy_sales_service.create_proxy_order', _async_return(order))
    monkeypatch.setattr('app.services.proxy_sales_service.reserve_proxy_stock_items', _async_return([]))
    monkeypatch.setattr('app.services.proxy_sales_service.create_proxy_provider_purchase', _async_return(purchase))
    monkeypatch.setattr('app.services.proxy_sales_service.complete_proxy_provider_purchase', _async_passthrough())
    monkeypatch.setattr('app.services.proxy_sales_service.subtract_user_balance', _async_return(True))
    monkeypatch.setattr('app.services.proxy_sales_service.create_transaction', _async_return(transaction))
    monkeypatch.setattr('app.services.proxy_sales_service.mark_proxy_order_paid', _async_passthrough())
    monkeypatch.setattr('app.services.proxy_sales_service.mark_proxy_order_fulfilled', _async_passthrough())
    monkeypatch.setattr('app.services.proxy_sales_service.emit_transaction_side_effects', _async_return(None))
    monkeypatch.setattr('app.services.proxy_sales_service.attach_stock_item_to_order', _async_return(None))
    monkeypatch.setattr(
        proxy_sales_service,
        '_materialize_provider_items',
        _async_return([created_item]),
    )

    result = await proxy_sales_service.purchase_product(db, user=user, product=product, quantity=1, bot=None)

    assert result.items == [created_item]
    assert result.order.provider_purchase_id == 44
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_purchase_product_raises_when_balance_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb()
    user = SimpleNamespace(id=1, telegram_id=111, balance_kopeks=5000)
    product = ProxyProduct(
        id=7,
        name='SOCKS',
        provider_category_id='abc',
        source_mode=ProxyProductSourceMode.STOCK_FIRST.value,
        markup_type='fixed',
        markup_value=2000,
        min_quantity=1,
        max_quantity=5,
        is_active=True,
    )
    stock_item = SimpleNamespace(id=10, unit_cost_kopeks=9000, get_delivery_line=lambda: '1.1.1.1:1080')
    order = SimpleNamespace(id=22, quantity=1, total_price_kopeks=11000, user_id=user.id, status='pending')

    released: list[list] = []

    class _FakeClient:
        async def get_category(self, category_id: str) -> ProxySoxyCategory:
            return ProxySoxyCategory(id='abc', name='RU', unit_cost_kopeks=9000)

    monkeypatch.setattr(proxy_sales_service, 'get_client', lambda: _FakeClient())
    monkeypatch.setattr('app.services.proxy_sales_service.get_setting_value', _async_return(None))
    monkeypatch.setattr('app.services.proxy_sales_service.create_proxy_order', _async_return(order))
    monkeypatch.setattr('app.services.proxy_sales_service.reserve_proxy_stock_items', _async_return([stock_item]))
    monkeypatch.setattr('app.services.proxy_sales_service.fail_proxy_order', _async_passthrough())

    async def _release(db_obj, items):
        released.append(list(items))

    monkeypatch.setattr('app.services.proxy_sales_service.release_reserved_proxy_stock_items', _release)

    with pytest.raises(ProxyInsufficientBalanceError):
        await proxy_sales_service.purchase_product(db, user=user, product=product, quantity=1, bot=None)

    assert released == [[stock_item]]
    assert db.commit_calls == 1


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def _async_passthrough():
    async def _inner(*args, **kwargs):
        if args:
            return args[1] if len(args) > 1 else args[0]
        return None

    return _inner
