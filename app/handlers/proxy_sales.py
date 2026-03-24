from __future__ import annotations

import html

from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.proxy_sales import (
    count_proxy_stock_for_product,
    get_active_proxy_products,
    get_proxy_order_by_id,
    get_proxy_product_by_id,
)
from app.database.models import User
from app.keyboards.inline import get_payment_methods_keyboard_with_cart
from app.localization.texts import get_texts
from app.services.proxy_sales_service import (
    ProxyInsufficientBalanceError,
    ProxySalesError,
    proxy_sales_service,
)
from app.services.user_cart_service import user_cart_service
from app.states import SubscriptionStates
from app.utils.decorators import error_handler


def _catalog_keyboard(products: list, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = [
        [types.InlineKeyboardButton(text=product.name, callback_data=f'proxy_product:{product.id}')] for product in products
    ]
    rows.append([types.InlineKeyboardButton(text='📦 Мои прокси', callback_data='proxy_orders')])
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _product_keyboard(product_id: int, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='✍️ Указать количество', callback_data=f'proxy_enter_quantity:{product_id}')],
            [types.InlineKeyboardButton(text='📦 Мои прокси', callback_data='proxy_orders')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_proxy_products')],
        ]
    )


def _confirm_keyboard(product_id: int, quantity: int, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='✅ Подтвердить покупку', callback_data=f'proxy_confirm:{product_id}:{quantity}')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=f'proxy_product:{product_id}')],
        ]
    )


def _orders_keyboard(orders: list, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for order in orders:
        product_name = order.product.name if order.product else f'Товар #{order.product_id or "?"}'
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'#{order.id} • {product_name}',
                    callback_data=f'proxy_order:{order.id}',
                )
            ]
        )
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_proxy_products')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _order_keyboard(order_id: int, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='📦 Мои прокси', callback_data='proxy_orders')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_proxy_products')],
        ]
    )


async def _render_purchase_preview(
    message: types.Message,
    *,
    language: str,
    product,
    quantity: int,
    balance_kopeks: int,
    db: AsyncSession,
    use_edit: bool,
) -> None:
    quote = await proxy_sales_service.get_product_quote(db, product, quantity)
    texts = get_texts(language)
    text = (
        f'🛒 <b>Подтверждение покупки</b>\n\n'
        f'Товар: <b>{html.escape(product.name)}</b>\n'
        f'Количество: <b>{quantity}</b>\n'
        f'Цена за 1 шт.: <b>{texts.format_price(quote.unit_price_kopeks)}</b>\n'
        f'Итого: <b>{texts.format_price(quote.total_price_kopeks)}</b>\n'
        f'Баланс: <b>{texts.format_price(balance_kopeks)}</b>'
    )
    if use_edit:
        await message.edit_text(
            text,
            reply_markup=_confirm_keyboard(product.id, quantity, language),
            parse_mode='HTML',
        )
        return
    await message.answer(
        text,
        reply_markup=_confirm_keyboard(product.id, quantity, language),
        parse_mode='HTML',
    )


@error_handler
async def show_proxy_catalog(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    if not await proxy_sales_service.is_sales_enabled(db):
        await callback.message.edit_text(
            texts.t('PROXY_SALES_DISABLED', '🧩 Продажа прокси временно отключена.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')]]
            ),
        )
        await callback.answer()
        return
    products = await get_active_proxy_products(db)
    if not products:
        await callback.message.edit_text(
            texts.t('PROXY_CATALOG_EMPTY', '🧩 Каталог прокси пока пуст.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')]]
            ),
        )
        await callback.answer()
        return

    lines = [texts.t('PROXY_CATALOG_TITLE', '🧩 <b>Каталог прокси</b>'), '']
    try:
        categories = {item.id: item for item in await proxy_sales_service.get_client().get_categories()}
    except Exception:
        categories = {}

    for product in products:
        stock_count = await count_proxy_stock_for_product(db, product.id)
        category = categories.get(product.provider_category_id)
        if category:
            unit_price = product.calculate_sale_price_kopeks(category.unit_cost_kopeks)
            price_line = texts.t('PROXY_CATALOG_PRICE_FROM', 'Цена от {price} за 1 шт.').format(
                price=texts.format_price(unit_price)
            )
        else:
            price_line = texts.t('PROXY_CATALOG_PRICE_UNKNOWN', 'Цена уточняется автоматически при покупке.')
        lines.append(f'• <b>{html.escape(product.name)}</b>')
        lines.append(f'  {price_line}')
        lines.append(f'  В наличии: <b>{stock_count}</b>')
        if product.description:
            lines.append(f'  {html.escape(product.description)}')
        lines.append('')

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=_catalog_keyboard(products, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def show_proxy_product(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    if not await proxy_sales_service.is_sales_enabled(db):
        await callback.answer('Продажа прокси отключена', show_alert=True)
        return
    product_id = int(callback.data.split(':', 1)[1])
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active:
        await callback.answer('Товар недоступен', show_alert=True)
        return

    texts = get_texts(db_user.language)
    stock_count = await count_proxy_stock_for_product(db, product.id)
    min_quantity, max_quantity = product.get_quantity_bounds()
    try:
        quote = await proxy_sales_service.get_product_quote(db, product, min_quantity)
        price_line = texts.t('PROXY_PRODUCT_PRICE_LINE', 'Цена за 1 шт.: {price}').format(
            price=texts.format_price(quote.unit_price_kopeks)
        )
    except Exception:
        price_line = texts.t('PROXY_PRODUCT_PRICE_UNAVAILABLE', 'Цена будет рассчитана при подтверждении покупки.')

    mode_map = {
        'stock_first': 'Сначала склад, затем автозакупка',
        'stock_only': 'Только со склада',
        'autobuy_only': 'Только автозакупка',
    }
    text = (
        f'🧩 <b>{html.escape(product.name)}</b>\n\n'
        f'{html.escape(product.description or "Описание не указано.")}\n\n'
        f'{price_line}\n'
        f'Режим выдачи: <b>{mode_map.get(product.source_mode, product.source_mode)}</b>\n'
        f'Количество: <b>от {min_quantity} до {max_quantity}</b>\n'
        f'Склад: <b>{stock_count}</b>'
    )
    await callback.message.edit_text(
        text,
        reply_markup=_product_keyboard(product.id, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def ask_proxy_quantity(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    if not await proxy_sales_service.is_sales_enabled(db):
        await callback.answer('Продажа прокси отключена', show_alert=True)
        return
    product_id = int(callback.data.split(':', 1)[1])
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active:
        await callback.answer('Товар недоступен', show_alert=True)
        return
    min_quantity, max_quantity = product.get_quantity_bounds()
    await state.set_state(SubscriptionStates.selecting_proxy_quantity)
    await state.update_data(proxy_product_id=product.id)
    await callback.message.edit_text(
        (
            f'✍️ <b>Введите количество</b>\n\n'
            f'Товар: <b>{html.escape(product.name)}</b>\n'
            f'Допустимый диапазон: <b>от {min_quantity} до {max_quantity}</b>'
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'proxy_product:{product.id}')]]
        ),
    )
    await callback.answer()


@error_handler
async def preview_proxy_purchase(callback: types.CallbackQuery, db_user: User, db: AsyncSession, *, product_id: int, quantity: int):
    product = await get_proxy_product_by_id(db, int(product_id))
    if product is None or not product.is_active:
        await callback.answer('Товар недоступен', show_alert=True)
        return
    await _render_purchase_preview(
        callback.message,
        language=db_user.language,
        product=product,
        quantity=quantity,
        balance_kopeks=db_user.balance_kopeks,
        db=db,
        use_edit=True,
    )
    await callback.answer()


@error_handler
async def receive_proxy_quantity(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    product_id = int(data.get('proxy_product_id') or 0)
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active:
        await state.clear()
        await message.answer('Товар недоступен.')
        return

    try:
        quantity = int((message.text or '').strip())
    except (TypeError, ValueError):
        min_quantity, max_quantity = product.get_quantity_bounds()
        await message.answer(f'Введите целое число от {min_quantity} до {max_quantity}.')
        return

    try:
        proxy_sales_service.validate_quantity(product, quantity)
    except ProxySalesError as error:
        await message.answer(str(error))
        return

    await state.clear()
    await _render_purchase_preview(
        message,
        language=db_user.language,
        product=product,
        quantity=quantity,
        balance_kopeks=db_user.balance_kopeks,
        db=db,
        use_edit=False,
    )


@error_handler
async def confirm_proxy_purchase(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    _, product_id_raw, quantity_raw = callback.data.split(':')
    product_id = int(product_id_raw)
    quantity = int(quantity_raw)
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active:
        await callback.answer('Товар недоступен', show_alert=True)
        return

    try:
        result = await proxy_sales_service.purchase_product(
            db,
            user=db_user,
            product=product,
            quantity=quantity,
            bot=callback.bot,
        )
    except ProxyInsufficientBalanceError as error:
        await user_cart_service.save_user_cart(
            db_user.id,
            {
                'saved_cart': True,
                'cart_mode': 'proxy_purchase',
                'product_id': product.id,
                'quantity': quantity,
                'missing_amount': error.missing_amount_kopeks,
                'total_price': error.required_amount_kopeks,
            },
        )
        await state.set_state(SubscriptionStates.cart_saved_for_topup)
        await state.update_data(saved_cart=True, total_price=error.required_amount_kopeks)
        texts = get_texts(db_user.language)
        await callback.message.edit_text(
            (
                f'💰 Недостаточно средств для покупки прокси\n\n'
                f'Требуется: {texts.format_price(error.required_amount_kopeks)}\n'
                f'Не хватает: {texts.format_price(error.missing_amount_kopeks)}\n\n'
                f'Корзина сохранена. После пополнения можно вернуться к оформлению.'
            ),
            reply_markup=get_payment_methods_keyboard_with_cart(
                db_user.language,
                amount_kopeks=error.missing_amount_kopeks,
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return
    except ProxySalesError as error:
        await callback.answer(str(error), show_alert=True)
        return

    await user_cart_service.delete_user_cart(db_user.id)
    delivery_lines = '\n'.join(f'<code>{html.escape(line)}</code>' for line in result.get_delivery_lines())
    text = (
        f'✅ <b>Покупка выполнена</b>\n\n'
        f'Заказ: <b>#{result.order.id}</b>\n'
        f'Товар: <b>{html.escape(product.name)}</b>\n'
        f'Количество: <b>{quantity}</b>\n'
        f'Списано: <b>{get_texts(db_user.language).format_price(result.quote.total_price_kopeks)}</b>\n\n'
        f'<b>Выданные прокси:</b>\n{delivery_lines}'
    )
    await callback.message.edit_text(
        text,
        reply_markup=_order_keyboard(result.order.id, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer('Прокси выданы')


@error_handler
async def show_proxy_orders(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    orders = await proxy_sales_service.list_user_orders(db, db_user.id)
    if not orders:
        await callback.message.edit_text(
            texts.t('PROXY_ORDERS_EMPTY', '📦 У вас пока нет заказов на прокси.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_proxy_products')]]
            ),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        '📦 <b>Мои прокси</b>\n\nВыберите заказ:',
        reply_markup=_orders_keyboard(orders[:20], db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def show_proxy_order_details(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    order_id = int(callback.data.split(':', 1)[1])
    order = await get_proxy_order_by_id(db, order_id)
    if order is None or order.user_id != db_user.id:
        await callback.answer('Заказ не найден', show_alert=True)
        return
    product_name = order.product.name if order.product else f'Товар #{order.product_id or "?"}'
    delivery_lines = '\n'.join(
        f'• <code>{html.escape(item.stock_item.get_delivery_line())}</code>'
        for item in order.items
        if item.stock_item is not None
    )
    text = (
        f'📦 <b>Заказ #{order.id}</b>\n\n'
        f'Товар: <b>{html.escape(product_name)}</b>\n'
        f'Статус: <b>{order.status}</b>\n'
        f'Количество: <b>{order.quantity}</b>\n'
        f'Сумма: <b>{get_texts(db_user.language).format_price(order.total_price_kopeks)}</b>\n\n'
        f'<b>Прокси:</b>\n{delivery_lines or "—"}'
    )
    await callback.message.edit_text(
        text,
        reply_markup=_order_keyboard(order.id, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


async def return_to_saved_proxy_cart(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
    cart_data: dict | None = None,
):
    saved = cart_data or await user_cart_service.get_user_cart(db_user.id)
    if not saved:
        await callback.answer('Сохраненная корзина не найдена', show_alert=True)
        return

    product_id = int(saved.get('product_id') or 0)
    quantity = int(saved.get('quantity') or 0)
    product = await get_proxy_product_by_id(db, product_id)
    if product is None or not product.is_active:
        await user_cart_service.delete_user_cart(db_user.id)
        await callback.answer('Товар больше недоступен', show_alert=True)
        return

    await state.set_state(SubscriptionStates.cart_saved_for_topup)
    await _render_purchase_preview(
        callback.message,
        language=db_user.language,
        product=product,
        quantity=quantity,
        balance_kopeks=db_user.balance_kopeks,
        db=db,
        use_edit=True,
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_proxy_catalog, F.data == 'menu_proxy_products')
    dp.callback_query.register(show_proxy_product, F.data.startswith('proxy_product:'))
    dp.callback_query.register(ask_proxy_quantity, F.data.startswith('proxy_enter_quantity:'))
    dp.callback_query.register(confirm_proxy_purchase, F.data.startswith('proxy_confirm:'))
    dp.callback_query.register(show_proxy_orders, F.data == 'proxy_orders')
    dp.callback_query.register(show_proxy_order_details, F.data.startswith('proxy_order:'))
    dp.message.register(receive_proxy_quantity, StateFilter(SubscriptionStates.selecting_proxy_quantity))
