from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    ProxyMarkupType,
    ProxyOrder,
    ProxyOrderItem,
    ProxyOrderStatus,
    ProxyProduct,
    ProxyProductSourceMode,
    ProxyProviderPurchase,
    ProxyProviderPurchaseStatus,
    ProxyProviderPurchaseType,
    ProxyStockItem,
    ProxyStockStatus,
)


async def get_proxy_product_by_id(db: AsyncSession, product_id: int) -> ProxyProduct | None:
    result = await db.execute(select(ProxyProduct).where(ProxyProduct.id == product_id))
    return result.scalar_one_or_none()


async def get_active_proxy_products(db: AsyncSession) -> list[ProxyProduct]:
    result = await db.execute(
        select(ProxyProduct)
        .where(ProxyProduct.is_active.is_(True), ProxyProduct.is_visible_in_catalog.is_(True))
        .order_by(ProxyProduct.display_order.asc(), ProxyProduct.id.asc())
    )
    return list(result.scalars().all())


async def list_proxy_products(db: AsyncSession) -> list[ProxyProduct]:
    result = await db.execute(select(ProxyProduct).order_by(ProxyProduct.display_order.asc(), ProxyProduct.id.asc()))
    return list(result.scalars().all())


async def create_proxy_product(
    db: AsyncSession,
    *,
    name: str,
    provider_category_id: str,
    description: str | None = None,
    provider_category_name: str | None = None,
    display_order: int = 0,
    is_active: bool = True,
    source_mode: ProxyProductSourceMode = ProxyProductSourceMode.STOCK_FIRST,
    markup_type: ProxyMarkupType = ProxyMarkupType.FIXED,
    markup_value: int = 0,
    min_quantity: int = 1,
    max_quantity: int = 1,
    is_visible_in_catalog: bool = True,
    metadata_json: dict[str, Any] | None = None,
) -> ProxyProduct:
    normalized_source_mode = source_mode.value if isinstance(source_mode, ProxyProductSourceMode) else str(source_mode)
    normalized_markup_type = markup_type.value if isinstance(markup_type, ProxyMarkupType) else str(markup_type)
    normalized_min_quantity = max(1, int(min_quantity))
    normalized_max_quantity = max(normalized_min_quantity, int(max_quantity))

    product = ProxyProduct(
        name=name,
        description=description,
        provider_category_id=str(provider_category_id),
        provider_category_name=provider_category_name,
        display_order=display_order,
        is_active=is_active,
        source_mode=normalized_source_mode,
        markup_type=normalized_markup_type,
        markup_value=max(0, int(markup_value)),
        min_quantity=normalized_min_quantity,
        max_quantity=normalized_max_quantity,
        is_visible_in_catalog=is_visible_in_catalog,
        metadata_json=metadata_json,
    )
    db.add(product)
    await db.flush()
    return product


async def update_proxy_product(db: AsyncSession, product: ProxyProduct, **fields: Any) -> ProxyProduct:
    if 'min_quantity' in fields or 'max_quantity' in fields:
        min_quantity = fields.get('min_quantity', product.min_quantity)
        max_quantity = fields.get('max_quantity', product.max_quantity)
        normalized_min_quantity = max(1, int(min_quantity))
        normalized_max_quantity = max(normalized_min_quantity, int(max_quantity))
        fields['min_quantity'] = normalized_min_quantity
        fields['max_quantity'] = normalized_max_quantity
    for key, value in fields.items():
        if hasattr(product, key):
            setattr(product, key, value)
    await db.flush()
    return product


async def delete_proxy_product(db: AsyncSession, product: ProxyProduct) -> None:
    await db.delete(product)
    await db.flush()


async def count_proxy_stock_for_product(
    db: AsyncSession,
    product_id: int,
    *,
    status: ProxyStockStatus = ProxyStockStatus.IN_STOCK,
) -> int:
    result = await db.execute(
        select(func.count(ProxyStockItem.id)).where(
            ProxyStockItem.product_id == product_id,
            ProxyStockItem.status == status.value,
        )
    )
    return int(result.scalar_one() or 0)


async def get_min_proxy_stock_unit_cost_kopeks(db: AsyncSession, product_id: int) -> int | None:
    result = await db.execute(
        select(func.min(ProxyStockItem.unit_cost_kopeks)).where(
            ProxyStockItem.product_id == product_id,
            ProxyStockItem.status == ProxyStockStatus.IN_STOCK.value,
        )
    )
    value = result.scalar_one_or_none()
    return int(value) if value is not None else None


async def reserve_proxy_stock_items(
    db: AsyncSession,
    *,
    product_id: int,
    quantity: int,
    order_id: int | None = None,
) -> list[ProxyStockItem]:
    result = await db.execute(
        select(ProxyStockItem)
        .where(
            ProxyStockItem.product_id == product_id,
            ProxyStockItem.status == ProxyStockStatus.IN_STOCK.value,
        )
        .order_by(ProxyStockItem.id.asc())
        .limit(max(0, quantity))
        .with_for_update(skip_locked=True)
    )
    items = list(result.scalars().all())
    for item in items:
        item.status = ProxyStockStatus.RESERVED.value
        item.reserved_for_order_id = order_id
    await db.flush()
    return items


async def release_reserved_proxy_stock_items(db: AsyncSession, items: Iterable[ProxyStockItem]) -> None:
    for item in items:
        item.status = ProxyStockStatus.IN_STOCK.value
        item.reserved_for_order_id = None
    await db.flush()


async def create_proxy_provider_purchase(
    db: AsyncSession,
    *,
    product_id: int | None,
    purchase_type: ProxyProviderPurchaseType,
    requested_quantity: int,
    user_id: int | None = None,
    unit_cost_kopeks: int = 0,
    total_cost_kopeks: int = 0,
    provider_order_id: str | None = None,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
) -> ProxyProviderPurchase:
    purchase = ProxyProviderPurchase(
        product_id=product_id,
        user_id=user_id,
        purchase_type=purchase_type.value,
        status=ProxyProviderPurchaseStatus.PENDING.value,
        requested_quantity=max(1, int(requested_quantity)),
        unit_cost_kopeks=max(0, int(unit_cost_kopeks)),
        total_cost_kopeks=max(0, int(total_cost_kopeks)),
        provider_order_id=provider_order_id,
        request_payload=request_payload,
        response_payload=response_payload,
    )
    db.add(purchase)
    await db.flush()
    return purchase


async def complete_proxy_provider_purchase(
    db: AsyncSession,
    purchase: ProxyProviderPurchase,
    *,
    fulfilled_quantity: int,
    response_payload: dict[str, Any] | None = None,
    provider_order_id: str | None = None,
) -> ProxyProviderPurchase:
    purchase.fulfilled_quantity = max(0, int(fulfilled_quantity))
    purchase.status = (
        ProxyProviderPurchaseStatus.COMPLETED.value
        if purchase.fulfilled_quantity >= purchase.requested_quantity
        else ProxyProviderPurchaseStatus.PARTIAL.value
    )
    purchase.completed_at = datetime.now(UTC)
    if response_payload is not None:
        purchase.response_payload = response_payload
    if provider_order_id:
        purchase.provider_order_id = provider_order_id
    await db.flush()
    return purchase


async def fail_proxy_provider_purchase(
    db: AsyncSession,
    purchase: ProxyProviderPurchase,
    *,
    error_message: str,
    response_payload: dict[str, Any] | None = None,
) -> ProxyProviderPurchase:
    purchase.status = ProxyProviderPurchaseStatus.FAILED.value
    purchase.error_message = error_message
    if response_payload is not None:
        purchase.response_payload = response_payload
    await db.flush()
    return purchase


async def create_proxy_stock_item(
    db: AsyncSession,
    *,
    product_id: int,
    unit_cost_kopeks: int,
    provider_purchase_id: int | None = None,
    provider_item_id: str | None = None,
    provider_order_id: str | None = None,
    endpoint: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    protocol: str | None = None,
    country: str | None = None,
    expires_at: datetime | None = None,
    raw_payload: dict[str, Any] | None = None,
    status: ProxyStockStatus = ProxyStockStatus.IN_STOCK,
) -> ProxyStockItem:
    item = ProxyStockItem(
        product_id=product_id,
        provider_purchase_id=provider_purchase_id,
        provider_item_id=provider_item_id,
        provider_order_id=provider_order_id,
        unit_cost_kopeks=max(0, int(unit_cost_kopeks)),
        endpoint=endpoint,
        host=host,
        port=port,
        username=username,
        password=password,
        protocol=protocol,
        country=country,
        expires_at=expires_at,
        raw_payload=raw_payload,
        status=status.value,
    )
    db.add(item)
    await db.flush()
    return item


async def create_proxy_order(
    db: AsyncSession,
    *,
    user_id: int,
    product_id: int | None,
    quantity: int,
    unit_price_kopeks: int,
    total_price_kopeks: int,
    total_cost_kopeks: int = 0,
    source_mode: str = ProxyProductSourceMode.STOCK_FIRST.value,
    transaction_id: int | None = None,
    provider_purchase_id: int | None = None,
) -> ProxyOrder:
    order = ProxyOrder(
        user_id=user_id,
        product_id=product_id,
        quantity=max(1, int(quantity)),
        unit_price_kopeks=max(0, int(unit_price_kopeks)),
        total_price_kopeks=max(0, int(total_price_kopeks)),
        total_cost_kopeks=max(0, int(total_cost_kopeks)),
        source_mode=source_mode,
        transaction_id=transaction_id,
        provider_purchase_id=provider_purchase_id,
    )
    db.add(order)
    await db.flush()
    return order


async def get_proxy_order_by_id(db: AsyncSession, order_id: int) -> ProxyOrder | None:
    result = await db.execute(
        select(ProxyOrder)
        .options(
            selectinload(ProxyOrder.product),
            selectinload(ProxyOrder.items).selectinload(ProxyOrderItem.stock_item),
            selectinload(ProxyOrder.user),
            selectinload(ProxyOrder.transaction),
        )
        .where(ProxyOrder.id == order_id)
    )
    return result.scalar_one_or_none()


async def list_proxy_orders(db: AsyncSession, *, limit: int = 50, offset: int = 0) -> list[ProxyOrder]:
    result = await db.execute(
        select(ProxyOrder)
        .options(selectinload(ProxyOrder.product), selectinload(ProxyOrder.user))
        .order_by(ProxyOrder.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_user_proxy_orders(db: AsyncSession, user_id: int, *, limit: int = 50, offset: int = 0) -> list[ProxyOrder]:
    result = await db.execute(
        select(ProxyOrder)
        .options(
            selectinload(ProxyOrder.product),
            selectinload(ProxyOrder.items).selectinload(ProxyOrderItem.stock_item),
        )
        .where(ProxyOrder.user_id == user_id)
        .order_by(ProxyOrder.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def attach_stock_item_to_order(
    db: AsyncSession,
    *,
    order: ProxyOrder,
    stock_item: ProxyStockItem,
    unit_price_kopeks: int,
    unit_cost_kopeks: int,
    is_replacement: bool = False,
    replaced_order_item_id: int | None = None,
) -> ProxyOrderItem:
    stock_item.status = ProxyStockStatus.SOLD.value
    stock_item.reserved_for_order_id = order.id
    stock_item.sold_at = datetime.now(UTC)
    order_item = ProxyOrderItem(
        order_id=order.id,
        stock_item_id=stock_item.id,
        unit_price_kopeks=max(0, int(unit_price_kopeks)),
        unit_cost_kopeks=max(0, int(unit_cost_kopeks)),
        is_replacement=is_replacement,
        replaced_order_item_id=replaced_order_item_id,
        delivered_at=datetime.now(UTC),
    )
    db.add(order_item)
    await db.flush()
    return order_item


async def mark_proxy_order_paid(
    db: AsyncSession,
    order: ProxyOrder,
    *,
    transaction_id: int,
) -> ProxyOrder:
    order.status = ProxyOrderStatus.PAID.value
    order.transaction_id = transaction_id
    order.paid_at = datetime.now(UTC)
    await db.flush()
    return order


async def mark_proxy_order_fulfilled(
    db: AsyncSession,
    order: ProxyOrder,
    *,
    delivered_quantity: int,
    delivery_payload: dict[str, Any] | None = None,
) -> ProxyOrder:
    order.delivered_quantity = max(0, int(delivered_quantity))
    order.status = (
        ProxyOrderStatus.FULFILLED.value
        if order.delivered_quantity >= order.quantity
        else ProxyOrderStatus.PARTIAL.value
    )
    order.fulfilled_at = datetime.now(UTC)
    if delivery_payload is not None:
        order.delivery_payload = delivery_payload
    await db.flush()
    return order


async def fail_proxy_order(db: AsyncSession, order: ProxyOrder, *, error_message: str) -> ProxyOrder:
    order.status = ProxyOrderStatus.FAILED.value
    order.error_message = error_message
    await db.flush()
    return order


async def mark_proxy_order_replacement_pending(db: AsyncSession, order: ProxyOrder) -> ProxyOrder:
    order.status = ProxyOrderStatus.REPLACEMENT_PENDING.value
    await db.flush()
    return order


async def mark_proxy_order_replaced(db: AsyncSession, order: ProxyOrder) -> ProxyOrder:
    order.status = ProxyOrderStatus.REPLACED.value
    await db.flush()
    return order
