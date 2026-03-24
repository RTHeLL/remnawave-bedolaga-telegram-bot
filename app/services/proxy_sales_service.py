from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.proxy_sales import (
    attach_stock_item_to_order,
    complete_proxy_provider_purchase,
    count_proxy_stock_for_product,
    create_proxy_order,
    create_proxy_product,
    create_proxy_provider_purchase,
    create_proxy_stock_item,
    fail_proxy_order,
    fail_proxy_provider_purchase,
    get_proxy_product_by_id,
    get_min_proxy_stock_unit_cost_kopeks,
    list_user_proxy_orders,
    mark_proxy_order_fulfilled,
    mark_proxy_order_paid,
    mark_proxy_order_replaced,
    mark_proxy_order_replacement_pending,
    release_reserved_proxy_stock_items,
    reserve_proxy_stock_items,
    update_proxy_product,
)
from app.database.crud.system_setting import get_setting_value
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.crud.user import subtract_user_balance
from app.database.models import (
    PaymentMethod,
    ProxyOrder,
    ProxyOrderItem,
    ProxyProduct,
    ProxyProductSourceMode,
    ProxyProviderPurchaseType,
    ProxyStockItem,
    ProxyStockStatus,
    TransactionType,
    User,
)
from app.external.proxysoxy_api import ProxySoxyCategory, ProxySoxyClient, ProxySoxyOrderResult
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory


logger = structlog.get_logger(__name__)


class ProxySalesError(RuntimeError):
    """Base error for proxy sales domain."""


class ProxySalesDisabledError(ProxySalesError):
    pass


class ProxyQuantityError(ProxySalesError):
    pass


class ProxyInsufficientBalanceError(ProxySalesError):
    def __init__(self, missing_amount_kopeks: int, required_amount_kopeks: int):
        self.missing_amount_kopeks = max(0, int(missing_amount_kopeks))
        self.required_amount_kopeks = max(0, int(required_amount_kopeks))
        super().__init__('Недостаточно средств для покупки прокси')


class ProxyOutOfStockError(ProxySalesError):
    pass


@dataclass(slots=True)
class ProxyProductQuote:
    product: ProxyProduct
    category: ProxySoxyCategory
    quantity: int
    unit_cost_kopeks: int
    unit_price_kopeks: int
    total_cost_kopeks: int
    total_price_kopeks: int
    currency: str


@dataclass(slots=True)
class ProxyPurchaseResult:
    order: ProxyOrder
    transaction_id: int
    quote: ProxyProductQuote
    items: list[ProxyStockItem]

    def get_delivery_lines(self) -> list[str]:
        return [item.get_delivery_line() for item in self.items]


class ProxySalesService:
    SALES_ENABLED_KEY = 'PROXY_SALES_ENABLED'
    AUTOBUY_ENABLED_KEY = 'PROXY_SALES_AUTOBUY_ENABLED'
    LOW_BALANCE_THRESHOLD_KEY = 'PROXY_SALES_LOW_BALANCE_THRESHOLD_KOPEKS'

    async def is_sales_enabled(self, db: AsyncSession) -> bool:
        raw = await get_setting_value(db, self.SALES_ENABLED_KEY)
        if raw is None:
            return True
        return str(raw).strip().lower() in {'1', 'true', 'yes', 'on'}

    async def is_autobuy_enabled(self, db: AsyncSession) -> bool:
        raw = await get_setting_value(db, self.AUTOBUY_ENABLED_KEY)
        if raw is None:
            return True
        return str(raw).strip().lower() in {'1', 'true', 'yes', 'on'}

    async def get_low_balance_threshold_kopeks(self, db: AsyncSession) -> int:
        raw = await get_setting_value(db, self.LOW_BALANCE_THRESHOLD_KEY)
        if raw is None:
            return settings.get_proxysoxy_low_balance_threshold_kopeks()
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return settings.get_proxysoxy_low_balance_threshold_kopeks()

    def get_client(self) -> ProxySoxyClient:
        return ProxySoxyClient()

    def validate_quantity(self, product: ProxyProduct, quantity: int) -> None:
        if quantity <= 0:
            raise ProxyQuantityError('Количество должно быть больше нуля')
        min_quantity, max_quantity = product.get_quantity_bounds()
        if not product.is_quantity_allowed(quantity):
            raise ProxyQuantityError(f'Количество должно быть от {min_quantity} до {max_quantity}')

    async def get_product_quote(
        self,
        db: AsyncSession,
        product: ProxyProduct,
        quantity: int,
        *,
        client: ProxySoxyClient | None = None,
    ) -> ProxyProductQuote:
        if not await self.is_sales_enabled(db):
            raise ProxySalesDisabledError('Продажа прокси отключена')
        self.validate_quantity(product, quantity)
        proxysoxy = client or self.get_client()
        try:
            category = await proxysoxy.get_category(product.provider_category_id)
            unit_cost_kopeks = max(0, category.unit_cost_kopeks)
        except Exception:
            stock_unit_cost_kopeks = await get_min_proxy_stock_unit_cost_kopeks(db, product.id)
            if stock_unit_cost_kopeks is None:
                raise
            category = ProxySoxyCategory(
                id=str(product.provider_category_id),
                name=product.provider_category_name or product.name,
                unit_cost_kopeks=stock_unit_cost_kopeks,
                currency='RUB',
                raw={'source': 'stock_fallback'},
            )
            unit_cost_kopeks = stock_unit_cost_kopeks
        unit_price_kopeks = product.calculate_sale_price_kopeks(unit_cost_kopeks)
        return ProxyProductQuote(
            product=product,
            category=category,
            quantity=quantity,
            unit_cost_kopeks=unit_cost_kopeks,
            unit_price_kopeks=unit_price_kopeks,
            total_cost_kopeks=unit_cost_kopeks * quantity,
            total_price_kopeks=unit_price_kopeks * quantity,
            currency=category.currency or 'RUB',
        )

    async def get_proxysoxy_status(self, db: AsyncSession) -> dict[str, Any]:
        client = self.get_client()
        configured = client.is_configured
        response: dict[str, Any] = {
            'configured': configured,
            'enabled': settings.is_proxysoxy_enabled(),
            'sales_enabled': await self.is_sales_enabled(db),
            'autobuy_enabled': await self.is_autobuy_enabled(db),
        }
        if not configured:
            return response
        balance_kopeks, currency, user_info = await client.get_balance()
        threshold = await self.get_low_balance_threshold_kopeks(db)
        response.update(
            {
                'balance_kopeks': balance_kopeks,
                'currency': currency,
                'user_info': user_info,
                'low_balance_threshold_kopeks': threshold,
                'is_low_balance': threshold > 0 and balance_kopeks <= threshold,
                'masked_api_key': _mask_secret(client.api_key),
            }
        )
        return response

    async def buy_stock(
        self,
        db: AsyncSession,
        *,
        product: ProxyProduct,
        quantity: int,
        bot: Bot | None = None,
    ) -> tuple[int, list[ProxyStockItem]]:
        if quantity <= 0:
            raise ProxyQuantityError('Количество для закупки должно быть больше нуля')
        client = self.get_client()
        category = await client.get_category(product.provider_category_id)
        unit_cost_kopeks = max(0, category.unit_cost_kopeks)
        purchase = await create_proxy_provider_purchase(
            db,
            product_id=product.id,
            purchase_type=ProxyProviderPurchaseType.STOCK,
            requested_quantity=quantity,
            unit_cost_kopeks=unit_cost_kopeks,
            total_cost_kopeks=unit_cost_kopeks * quantity,
            request_payload={'category_id': product.provider_category_id, 'quantity': quantity},
        )

        try:
            order_result = await client.create_order(category_id=product.provider_category_id, quantity=quantity)
            items = await self._materialize_provider_items(
                db,
                product=product,
                provider_purchase_id=purchase.id,
                order_result=order_result,
                reserved_for_order_id=None,
                reserve=False,
            )
            await complete_proxy_provider_purchase(
                db,
                purchase,
                fulfilled_quantity=len(items),
                response_payload=order_result.raw,
                provider_order_id=order_result.order_id,
            )
            await db.commit()
            if bot:
                await self._notify_admins_stock_purchase(bot, product, items, purchase.total_cost_kopeks)
            return purchase.id, items
        except Exception as error:
            await fail_proxy_provider_purchase(db, purchase, error_message=str(error))
            await db.commit()
            raise

    async def purchase_product(
        self,
        db: AsyncSession,
        *,
        user: User,
        product: ProxyProduct,
        quantity: int,
        bot: Bot | None = None,
    ) -> ProxyPurchaseResult:
        client = self.get_client()
        quote = await self.get_product_quote(db, product, quantity, client=client)

        order = await create_proxy_order(
            db,
            user_id=user.id,
            product_id=product.id,
            quantity=quantity,
            unit_price_kopeks=quote.unit_price_kopeks,
            total_price_kopeks=quote.total_price_kopeks,
            total_cost_kopeks=0,
            source_mode=product.source_mode,
        )

        reserved_stock: list[ProxyStockItem] = []
        if product.source_mode != ProxyProductSourceMode.AUTOBUY_ONLY.value:
            reserved_stock = await reserve_proxy_stock_items(db, product_id=product.id, quantity=quantity, order_id=order.id)
        created_stock: list[ProxyStockItem] = []
        provider_purchase_id: int | None = None
        missing_quantity = max(0, quantity - len(reserved_stock))

        if missing_quantity > 0:
            if product.source_mode == ProxyProductSourceMode.STOCK_ONLY.value:
                await release_reserved_proxy_stock_items(db, reserved_stock)
                await fail_proxy_order(db, order, error_message='Недостаточно товара на складе')
                await db.commit()
                raise ProxyOutOfStockError('Недостаточно товара на складе')

            if not await self.is_autobuy_enabled(db):
                await release_reserved_proxy_stock_items(db, reserved_stock)
                await fail_proxy_order(db, order, error_message='Автозакупка прокси отключена')
                await db.commit()
                raise ProxyOutOfStockError('Автозакупка прокси отключена')

            purchase = await create_proxy_provider_purchase(
                db,
                product_id=product.id,
                user_id=user.id,
                purchase_type=ProxyProviderPurchaseType.ORDER,
                requested_quantity=missing_quantity,
                unit_cost_kopeks=quote.unit_cost_kopeks,
                total_cost_kopeks=quote.unit_cost_kopeks * missing_quantity,
                request_payload={'category_id': product.provider_category_id, 'quantity': missing_quantity},
            )
            provider_purchase_id = purchase.id
            order.provider_purchase_id = purchase.id

            try:
                provider_result = await client.create_order(
                    category_id=product.provider_category_id,
                    quantity=missing_quantity,
                )
                created_stock = await self._materialize_provider_items(
                    db,
                    product=product,
                    provider_purchase_id=purchase.id,
                    order_result=provider_result,
                    reserved_for_order_id=order.id,
                    reserve=True,
                )
                await complete_proxy_provider_purchase(
                    db,
                    purchase,
                    fulfilled_quantity=len(created_stock),
                    response_payload=provider_result.raw,
                    provider_order_id=provider_result.order_id,
                )
            except Exception as error:
                await release_reserved_proxy_stock_items(db, reserved_stock)
                await fail_proxy_provider_purchase(db, purchase, error_message=str(error))
                await fail_proxy_order(db, order, error_message=f'Ошибка автозакупки у поставщика: {error}')
                await db.commit()
                raise

        final_stock = reserved_stock + created_stock
        total_cost_kopeks = sum(item.unit_cost_kopeks for item in final_stock)
        order.total_cost_kopeks = total_cost_kopeks
        order.provider_purchase_id = provider_purchase_id

        if user.balance_kopeks < quote.total_price_kopeks:
            await release_reserved_proxy_stock_items(db, final_stock)
            await fail_proxy_order(db, order, error_message='Недостаточно средств на балансе')
            await db.commit()
            raise ProxyInsufficientBalanceError(quote.total_price_kopeks - user.balance_kopeks, quote.total_price_kopeks)

        description = f'Покупка прокси: {product.name} x{quantity}'
        debited = await subtract_user_balance(
            db,
            user,
            quote.total_price_kopeks,
            description,
            create_transaction=False,
            payment_method=PaymentMethod.BALANCE,
            transaction_type=TransactionType.PROXY_PURCHASE,
            commit=False,
        )
        if not debited:
            await release_reserved_proxy_stock_items(db, final_stock)
            await fail_proxy_order(db, order, error_message='Не удалось списать баланс пользователя')
            await db.commit()
            raise ProxySalesError('Не удалось списать баланс пользователя')

        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.PROXY_PURCHASE,
            amount_kopeks=quote.total_price_kopeks,
            description=description,
            payment_method=PaymentMethod.BALANCE,
            commit=False,
        )

        await mark_proxy_order_paid(db, order, transaction_id=transaction.id)
        for item in final_stock:
            await attach_stock_item_to_order(
                db,
                order=order,
                stock_item=item,
                unit_price_kopeks=quote.unit_price_kopeks,
                unit_cost_kopeks=item.unit_cost_kopeks,
            )

        delivery_payload = {'lines': [item.get_delivery_line() for item in final_stock]}
        await mark_proxy_order_fulfilled(db, order, delivered_quantity=len(final_stock), delivery_payload=delivery_payload)
        await db.commit()
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=quote.total_price_kopeks,
            user_id=user.id,
            type=TransactionType.PROXY_PURCHASE,
            payment_method=PaymentMethod.BALANCE,
            description=description,
        )

        if bot:
            await self._notify_admins_user_purchase(bot, user, product, quantity, quote.total_price_kopeks)

        return ProxyPurchaseResult(
            order=order,
            transaction_id=transaction.id,
            quote=quote,
            items=final_stock,
        )

    async def replace_order_item(
        self,
        db: AsyncSession,
        *,
        order_item_id: int,
        bot: Bot | None = None,
    ) -> ProxyOrderItem:
        order = await self._get_order_for_replacement(db, order_item_id)
        if not order.items:
            raise ProxySalesError('Заказ не содержит выданных прокси')
        target_item = next((item for item in order.items if item.id == order_item_id), None)
        if target_item is None:
            raise ProxySalesError('Позиция заказа не найдена')

        product = order.product
        if product is None:
            raise ProxySalesError('Для заказа не найден товар прокси')

        await mark_proxy_order_replacement_pending(db, order)
        reserved = await reserve_proxy_stock_items(db, product_id=product.id, quantity=1, order_id=order.id)
        replacement_item: ProxyStockItem | None = reserved[0] if reserved else None

        if replacement_item is None and product.source_mode != ProxyProductSourceMode.STOCK_ONLY.value:
            client = self.get_client()
            purchase = await create_proxy_provider_purchase(
                db,
                product_id=product.id,
                user_id=order.user_id,
                purchase_type=ProxyProviderPurchaseType.REPLACEMENT,
                requested_quantity=1,
            )
            provider_result = await client.create_order(category_id=product.provider_category_id, quantity=1)
            created = await self._materialize_provider_items(
                db,
                product=product,
                provider_purchase_id=purchase.id,
                order_result=provider_result,
                reserved_for_order_id=order.id,
                reserve=True,
            )
            replacement_item = created[0] if created else None
            await complete_proxy_provider_purchase(
                db,
                purchase,
                fulfilled_quantity=len(created),
                response_payload=provider_result.raw,
                provider_order_id=provider_result.order_id,
            )

        if replacement_item is None:
            await fail_proxy_order(db, order, error_message='Не удалось найти прокси для замены')
            await db.commit()
            raise ProxyOutOfStockError('Нет доступного прокси для замены')

        target_item.stock_item.status = ProxyStockStatus.REPLACED.value
        replacement_item.status = ProxyStockStatus.SOLD.value
        replacement_item.reserved_for_order_id = order.id
        new_order_item = await attach_stock_item_to_order(
            db,
            order=order,
            stock_item=replacement_item,
            unit_price_kopeks=target_item.unit_price_kopeks,
            unit_cost_kopeks=replacement_item.unit_cost_kopeks,
            is_replacement=True,
            replaced_order_item_id=target_item.id,
        )
        await mark_proxy_order_replaced(db, order)
        await db.commit()

        if bot:
            await self._notify_admins_replacement(bot, order, target_item, new_order_item)

        return new_order_item

    async def list_user_orders(self, db: AsyncSession, user_id: int) -> list[ProxyOrder]:
        return await list_user_proxy_orders(db, user_id)

    async def upsert_product(self, db: AsyncSession, **payload: Any) -> ProxyProduct:
        product_id = payload.pop('product_id', None)
        if product_id:
            product = await get_proxy_product_by_id(db, int(product_id))
            if product is None:
                raise ProxySalesError('Товар прокси не найден')
            return await update_proxy_product(db, product, **payload)
        return await create_proxy_product(db, **payload)

    async def get_stock_summary(self, db: AsyncSession, product: ProxyProduct) -> dict[str, int]:
        return {
            'in_stock': await count_proxy_stock_for_product(db, product.id, status=ProxyStockStatus.IN_STOCK),
            'sold': await count_proxy_stock_for_product(db, product.id, status=ProxyStockStatus.SOLD),
            'reserved': await count_proxy_stock_for_product(db, product.id, status=ProxyStockStatus.RESERVED),
            'replaced': await count_proxy_stock_for_product(db, product.id, status=ProxyStockStatus.REPLACED),
        }

    async def _materialize_provider_items(
        self,
        db: AsyncSession,
        *,
        product: ProxyProduct,
        provider_purchase_id: int,
        order_result: ProxySoxyOrderResult,
        reserved_for_order_id: int | None,
        reserve: bool,
    ) -> list[ProxyStockItem]:
        items = list(order_result.items)
        if not items and order_result.order_id:
            downloaded = await self.get_client().download_order(order_result.order_id)
            items = downloaded
        if not items:
            raise ProxySalesError('Поставщик не вернул список прокси для выдачи')

        created: list[ProxyStockItem] = []
        for item in items:
            created_item = await create_proxy_stock_item(
                db,
                product_id=product.id,
                provider_purchase_id=provider_purchase_id,
                provider_item_id=item.provider_item_id,
                provider_order_id=order_result.order_id,
                unit_cost_kopeks=order_result.unit_cost_kopeks or 0,
                endpoint=item.endpoint,
                host=item.host,
                port=item.port,
                username=item.username,
                password=item.password,
                protocol=item.protocol,
                country=item.country,
                expires_at=item.expires_at,
                raw_payload=item.raw,
                status=ProxyStockStatus.RESERVED if reserve else ProxyStockStatus.IN_STOCK,
            )
            if reserve:
                created_item.reserved_for_order_id = reserved_for_order_id
            created.append(created_item)
        await db.flush()
        return created

    async def _get_order_for_replacement(self, db: AsyncSession, order_item_id: int) -> ProxyOrder:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(ProxyOrder)
            .options(
                selectinload(ProxyOrder.product),
                selectinload(ProxyOrder.items).selectinload(ProxyOrderItem.stock_item),
                selectinload(ProxyOrder.user),
            )
            .join(ProxyOrder.items)
            .where(ProxyOrderItem.id == order_item_id)
        )
        order = result.scalar_one_or_none()
        if order is None:
            raise ProxySalesError('Заказ для замены не найден')
        return order

    async def _notify_admins_user_purchase(
        self,
        bot: Bot,
        user: User,
        product: ProxyProduct,
        quantity: int,
        total_price_kopeks: int,
    ) -> None:
        text = (
            '🛒 <b>Новая покупка прокси</b>\n\n'
            f'Пользователь: <code>{user.telegram_id or user.id}</code>\n'
            f'Товар: <b>{product.name}</b>\n'
            f'Количество: <b>{quantity}</b>\n'
            f'Сумма: <b>{settings.format_price(total_price_kopeks)}</b>'
        )
        await self._send_admin_notification(bot, text)

    async def _notify_admins_stock_purchase(
        self,
        bot: Bot,
        product: ProxyProduct,
        items: list[ProxyStockItem],
        total_cost_kopeks: int,
    ) -> None:
        text = (
            '📦 <b>Закупка прокси на склад</b>\n\n'
            f'Товар: <b>{product.name}</b>\n'
            f'Количество: <b>{len(items)}</b>\n'
            f'Себестоимость: <b>{settings.format_price(total_cost_kopeks)}</b>'
        )
        await self._send_admin_notification(bot, text)

    async def _notify_admins_replacement(
        self,
        bot: Bot,
        order: ProxyOrder,
        old_item: ProxyOrderItem,
        new_item: ProxyOrderItem,
    ) -> None:
        text = (
            '🔁 <b>Выполнена замена прокси</b>\n\n'
            f'Заказ: <b>#{order.id}</b>\n'
            f'Пользователь: <code>{order.user.telegram_id if order.user else order.user_id}</code>\n'
            f'Старая позиция: <b>#{old_item.id}</b>\n'
            f'Новая позиция: <b>#{new_item.id}</b>'
        )
        await self._send_admin_notification(bot, text)

    async def _send_admin_notification(self, bot: Bot, text: str) -> None:
        notification_service = AdminNotificationService(bot)
        if not notification_service.enabled:
            return
        try:
            await notification_service.send_plain_notification(
                text=text,
                category=NotificationCategory.PURCHASES,
            )
        except AttributeError:
            chat_id = notification_service.chat_id
            if not chat_id:
                return
            await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')


def _mask_secret(value: str | None) -> str:
    if not value:
        return '—'
    if len(value) <= 4:
        return '*' * len(value)
    return value[:2] + '*' * (len(value) - 4) + value[-2:]


proxy_sales_service = ProxySalesService()
