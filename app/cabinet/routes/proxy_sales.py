"""Cabinet routes for proxy sales."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.proxy_sales import (
    count_proxy_stock_for_product,
    get_active_proxy_products,
    get_proxy_order_by_id,
    get_proxy_product_by_id,
)
from app.database.crud.user import get_user_by_id
from app.database.models import ProxyOrder, User
from app.services.proxy_sales_service import (
    ProxyInsufficientBalanceError,
    ProxySalesError,
    proxy_sales_service,
)
from app.services.user_cart_service import user_cart_service

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.proxy_sales import (
    ProxyOrderItemResponse,
    ProxyOrderListResponse,
    ProxyOrderResponse,
    ProxyProductListResponse,
    ProxyProductResponse,
    ProxyPurchasePreviewRequest,
    ProxyPurchasePreviewResponse,
    ProxyPurchaseResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/proxy-sales', tags=['Cabinet Proxy Sales'])


def _serialize_order(order: ProxyOrder) -> ProxyOrderResponse:
    product_name = order.product.name if order.product else f'Товар #{order.product_id or "?"}'
    items: list[ProxyOrderItemResponse] = []
    delivery_lines: list[str] = []
    for item in order.items:
        if item.stock_item is None:
            continue
        delivery_line = item.stock_item.get_delivery_line()
        delivery_lines.append(delivery_line)
        items.append(
            ProxyOrderItemResponse(
                id=item.id,
                delivery_line=delivery_line,
                is_replacement=bool(item.is_replacement),
                delivered_at=item.delivered_at,
            )
        )

    return ProxyOrderResponse(
        id=order.id,
        product_id=order.product_id,
        product_name=product_name,
        status=order.status,
        quantity=order.quantity,
        delivered_quantity=order.delivered_quantity,
        unit_price_kopeks=order.unit_price_kopeks,
        total_price_kopeks=order.total_price_kopeks,
        total_cost_kopeks=order.total_cost_kopeks,
        currency=order.currency,
        created_at=order.created_at,
        paid_at=order.paid_at,
        fulfilled_at=order.fulfilled_at,
        error_message=order.error_message,
        delivery_lines=delivery_lines,
        items=items,
    )


async def _get_fresh_user_or_404(db: AsyncSession, user_id: int) -> User:
    user = await get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
    return user


@router.get('/products', response_model=ProxyProductListResponse)
async def list_proxy_products(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    del user
    sales_enabled = await proxy_sales_service.is_sales_enabled(db)
    products = await get_active_proxy_products(db) if sales_enabled else []
    items: list[ProxyProductResponse] = []

    for product in products:
        stock_count = await count_proxy_stock_for_product(db, product.id)
        unit_price_kopeks: int | None = None
        currency: str | None = None
        try:
            quote = await proxy_sales_service.get_product_quote(db, product, product.min_quantity)
            unit_price_kopeks = quote.unit_price_kopeks
            currency = quote.currency
        except Exception:
            logger.debug('Proxy product quote unavailable for cabinet list', product_id=product.id)

        items.append(
            ProxyProductResponse(
                id=product.id,
                name=product.name,
                description=product.description,
                provider_category_id=product.provider_category_id,
                provider_category_name=product.provider_category_name,
                source_mode=product.source_mode,
                markup_type=product.markup_type,
                markup_value=product.markup_value,
                min_quantity=product.min_quantity,
                max_quantity=product.max_quantity,
                is_active=product.is_active,
                is_visible_in_catalog=product.is_visible_in_catalog,
                in_stock_count=stock_count,
                unit_price_kopeks=unit_price_kopeks,
                currency=currency,
            )
        )

    return ProxyProductListResponse(sales_enabled=sales_enabled, items=items)


@router.get('/products/{product_id}', response_model=ProxyProductResponse)
async def get_proxy_product(
    product_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    del user
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active or not product.is_visible_in_catalog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Proxy product not found')

    stock_count = await count_proxy_stock_for_product(db, product.id)
    unit_price_kopeks: int | None = None
    currency: str | None = None
    try:
        quote = await proxy_sales_service.get_product_quote(db, product, product.min_quantity)
        unit_price_kopeks = quote.unit_price_kopeks
        currency = quote.currency
    except Exception:
        logger.debug('Proxy product quote unavailable for cabinet detail', product_id=product.id)

    return ProxyProductResponse(
        id=product.id,
        name=product.name,
        description=product.description,
        provider_category_id=product.provider_category_id,
        provider_category_name=product.provider_category_name,
        source_mode=product.source_mode,
        markup_type=product.markup_type,
        markup_value=product.markup_value,
        min_quantity=product.min_quantity,
        max_quantity=product.max_quantity,
        is_active=product.is_active,
        is_visible_in_catalog=product.is_visible_in_catalog,
        in_stock_count=stock_count,
        unit_price_kopeks=unit_price_kopeks,
        currency=currency,
    )


@router.post('/products/{product_id}/preview', response_model=ProxyPurchasePreviewResponse)
async def preview_proxy_purchase(
    product_id: int,
    request: ProxyPurchasePreviewRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    fresh_user = await _get_fresh_user_or_404(db, user.id)
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active or not product.is_visible_in_catalog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Proxy product not found')

    try:
        quote = await proxy_sales_service.get_product_quote(db, product, request.quantity)
    except ProxySalesError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    return ProxyPurchasePreviewResponse(
        product_id=product.id,
        quantity=request.quantity,
        unit_price_kopeks=quote.unit_price_kopeks,
        total_price_kopeks=quote.total_price_kopeks,
        currency=quote.currency,
        balance_kopeks=fresh_user.balance_kopeks,
        balance_after_purchase_kopeks=fresh_user.balance_kopeks - quote.total_price_kopeks,
    )


@router.post('/products/{product_id}/purchase', response_model=ProxyPurchaseResponse)
async def purchase_proxy_product(
    product_id: int,
    request: ProxyPurchasePreviewRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    fresh_user = await _get_fresh_user_or_404(db, user.id)
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active or not product.is_visible_in_catalog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Proxy product not found')

    try:
        result = await proxy_sales_service.purchase_product(
            db,
            user=fresh_user,
            product=product,
            quantity=request.quantity,
            bot=None,
        )
    except ProxyInsufficientBalanceError as error:
        await user_cart_service.save_user_cart(
            fresh_user.id,
            {
                'saved_cart': True,
                'cart_mode': 'proxy_purchase',
                'product_id': product.id,
                'quantity': request.quantity,
                'missing_amount': error.missing_amount_kopeks,
                'total_price': error.required_amount_kopeks,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                'code': 'insufficient_balance',
                'message': str(error),
                'missing_amount_kopeks': error.missing_amount_kopeks,
                'required_amount_kopeks': error.required_amount_kopeks,
            },
        ) from error
    except ProxySalesError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    await user_cart_service.delete_user_cart(fresh_user.id)
    refreshed_user = await _get_fresh_user_or_404(db, fresh_user.id)
    return ProxyPurchaseResponse(
        order=_serialize_order(result.order),
        balance_kopeks=refreshed_user.balance_kopeks,
        charged_amount_kopeks=result.quote.total_price_kopeks,
    )


@router.get('/orders', response_model=ProxyOrderListResponse)
async def list_proxy_orders(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    orders = await proxy_sales_service.list_user_orders(db, user.id)
    return ProxyOrderListResponse(items=[_serialize_order(order) for order in orders])


@router.get('/orders/{order_id}', response_model=ProxyOrderResponse)
async def get_proxy_order(
    order_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    order = await get_proxy_order_by_id(db, order_id)
    if order is None or order.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Proxy order not found')
    return _serialize_order(order)
