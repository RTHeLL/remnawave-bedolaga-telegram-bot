from __future__ import annotations

import html

from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.proxy_sales import (
    delete_proxy_product,
    get_proxy_order_by_id,
    get_proxy_product_by_id,
    list_proxy_orders,
    list_proxy_products,
)
from app.database.crud.system_setting import upsert_system_setting
from app.database.models import ProxyProduct, User
from app.localization.texts import get_texts
from app.services.proxy_sales_service import ProxySalesError, proxy_sales_service
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


def _parse_quantity_range(raw_value: str) -> tuple[int, int]:
    separators = (',', ';', '-', ':')
    normalized = raw_value
    for separator in separators:
        normalized = normalized.replace(separator, ' ')
    parts = [part.strip() for part in normalized.split() if part.strip()]
    if len(parts) != 2:
        raise ValueError('Нужно указать два числа: минимум и максимум')
    minimum = max(1, int(parts[0]))
    maximum = max(minimum, int(parts[1]))
    return minimum, maximum


def _menu_keyboard(language: str, *, sales_enabled: bool) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text='🟢 Продажа включена' if sales_enabled else '🔴 Продажа выключена',
                    callback_data='admin_proxy_toggle_sales',
                )
            ],
            [types.InlineKeyboardButton(text='📊 Статус ProxySoxy', callback_data='admin_proxy_status')],
            [types.InlineKeyboardButton(text='🧩 Товары', callback_data='admin_proxy_products')],
            [types.InlineKeyboardButton(text='📦 Заказы', callback_data='admin_proxy_orders')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def _products_keyboard(products: list[ProxyProduct], language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = [
        [
            types.InlineKeyboardButton(
                text=f'{"✅" if product.is_active else "⛔"} {product.name}',
                callback_data=f'admin_proxy_product:{product.id}',
            )
        ]
        for product in products
    ]
    rows.append([types.InlineKeyboardButton(text='➕ Создать товар', callback_data='admin_proxy_product_new')])
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_proxy_sales')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _product_keyboard(product_id: int, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text='✏️ Название', callback_data=f'admin_proxy_edit_name:{product_id}'),
                types.InlineKeyboardButton(text='📝 Описание', callback_data=f'admin_proxy_edit_description:{product_id}'),
            ],
            [
                types.InlineKeyboardButton(text='🏷️ Категория', callback_data=f'admin_proxy_edit_category:{product_id}'),
                types.InlineKeyboardButton(text='💹 Наценка', callback_data=f'admin_proxy_edit_markup:{product_id}'),
            ],
            [
                types.InlineKeyboardButton(text='🔢 Количества', callback_data=f'admin_proxy_edit_quantities:{product_id}'),
                types.InlineKeyboardButton(text='📦 Режим выдачи', callback_data=f'admin_proxy_edit_source:{product_id}'),
            ],
            [
                types.InlineKeyboardButton(text='🛒 Закупить на склад', callback_data=f'admin_proxy_stock_buy:{product_id}'),
                types.InlineKeyboardButton(text='🔄 Вкл/выкл', callback_data=f'admin_proxy_toggle:{product_id}'),
            ],
            [types.InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'admin_proxy_delete:{product_id}')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_proxy_products')],
        ]
    )


def _markup_type_keyboard(prefix: str, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text='Фиксированная', callback_data=f'{prefix}:fixed'),
                types.InlineKeyboardButton(text='Процентная', callback_data=f'{prefix}:percent'),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_proxy_products')],
        ]
    )


def _source_mode_keyboard(prefix: str, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='Склад -> автозакупка', callback_data=f'{prefix}:stock_first')],
            [types.InlineKeyboardButton(text='Только склад', callback_data=f'{prefix}:stock_only')],
            [types.InlineKeyboardButton(text='Только автозакупка', callback_data=f'{prefix}:autobuy_only')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_proxy_products')],
        ]
    )


def _orders_keyboard(orders, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for order in orders:
        product_name = order.product.name if order.product else f'Товар #{order.product_id or "?"}'
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'#{order.id} • {product_name} • {order.status}',
                    callback_data=f'admin_proxy_order:{order.id}',
                )
            ]
        )
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_proxy_sales')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _order_keyboard(order, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for item in order.items:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'🔁 Замена позиции #{item.id}',
                    callback_data=f'admin_proxy_replace_item:{item.id}',
                )
            ]
        )
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_proxy_orders')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _product_text(product: ProxyProduct, stock_summary: dict[str, int]) -> str:
    min_quantity, max_quantity = product.get_quantity_bounds()
    return (
        f'🧩 <b>{html.escape(product.name)}</b>\n\n'
        f'Описание: {html.escape(product.description or "—")}\n'
        f'Категория ProxySoxy: <code>{html.escape(product.provider_category_id)}</code>\n'
        f'Режим выдачи: <b>{html.escape(product.source_mode)}</b>\n'
        f'Наценка: <b>{html.escape(product.markup_type)} {product.markup_value}</b>\n'
        f'Диапазон количества: <b>от {min_quantity} до {max_quantity}</b>\n'
        f'Активен: <b>{"Да" if product.is_active else "Нет"}</b>\n\n'
        f'Склад:\n'
        f'• В наличии: <b>{stock_summary["in_stock"]}</b>\n'
        f'• Зарезервировано: <b>{stock_summary["reserved"]}</b>\n'
        f'• Продано: <b>{stock_summary["sold"]}</b>\n'
        f'• Заменено: <b>{stock_summary["replaced"]}</b>'
    )


@admin_required
@error_handler
async def show_proxy_sales_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    sales_enabled = await proxy_sales_service.is_sales_enabled(db)
    await callback.message.edit_text(
        '🧩 <b>Продажа прокси</b>\n\nВыберите раздел:',
        parse_mode='HTML',
        reply_markup=_menu_keyboard(db_user.language, sales_enabled=sales_enabled),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_proxy_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    status = await proxy_sales_service.get_proxysoxy_status(db)
    balance = status.get('balance_kopeks')
    text = (
        '📊 <b>Статус ProxySoxy</b>\n\n'
        f'Продажа пользователям: <b>{"Да" if status.get("sales_enabled") else "Нет"}</b>\n'
        f'Интеграция ProxySoxy: <b>{"Да" if status.get("enabled") else "Нет"}</b>\n'
        f'Настроено: <b>{"Да" if status.get("configured") else "Нет"}</b>\n'
        f'Автозакупка: <b>{"Да" if status.get("autobuy_enabled") else "Нет"}</b>\n'
        f'API Key: <b>{html.escape(status.get("masked_api_key", "—"))}</b>\n'
    )
    if balance is not None:
        text += (
            f'Баланс: <b>{get_texts(db_user.language).format_price(balance)}</b>\n'
            f'Порог тревоги: <b>{get_texts(db_user.language).format_price(status.get("low_balance_threshold_kopeks", 0))}</b>\n'
            f'Низкий баланс: <b>{"Да" if status.get("is_low_balance") else "Нет"}</b>\n'
        )
    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=get_texts(db_user.language).BACK, callback_data='admin_proxy_sales')]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_proxy_sales(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current_value = await proxy_sales_service.is_sales_enabled(db)
    new_value = not current_value
    await upsert_system_setting(
        db,
        proxy_sales_service.SALES_ENABLED_KEY,
        'true' if new_value else 'false',
        description='Глобальный переключатель продажи прокси пользователям',
    )
    await db.commit()
    await callback.message.edit_text(
        '🧩 <b>Продажа прокси</b>\n\nВыберите раздел:',
        parse_mode='HTML',
        reply_markup=_menu_keyboard(db_user.language, sales_enabled=new_value),
    )
    await callback.answer('Продажа прокси включена' if new_value else 'Продажа прокси отключена')


@admin_required
@error_handler
async def show_proxy_products(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    products = await list_proxy_products(db)
    await callback.message.edit_text(
        '🧩 <b>Товары прокси</b>\n\nВыберите товар или создайте новый.',
        parse_mode='HTML',
        reply_markup=_products_keyboard(products, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_proxy_product(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    product_id = int(callback.data.split(':', 1)[1])
    product = await get_proxy_product_by_id(db, product_id)
    if product is None:
        await callback.answer('Товар не найден', show_alert=True)
        return
    stock_summary = await proxy_sales_service.get_stock_summary(db, product)
    await callback.message.edit_text(
        _product_text(product, stock_summary),
        parse_mode='HTML',
        reply_markup=_product_keyboard(product.id, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_create_proxy_product(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    await state.clear()
    await state.set_state(AdminStates.creating_proxy_product_name)
    await state.update_data(proxy_product_payload={})
    await callback.message.edit_text('Введите название товара прокси:')
    await callback.answer()


@admin_required
@error_handler
async def receive_proxy_product_name(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    payload = dict(data.get('proxy_product_payload') or {})
    payload['name'] = message.text.strip()
    await state.update_data(proxy_product_payload=payload)
    await state.set_state(AdminStates.creating_proxy_product_description)
    await message.answer('Введите описание товара или `-`, если без описания.')


@admin_required
@error_handler
async def receive_proxy_product_description(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    payload = dict(data.get('proxy_product_payload') or {})
    payload['description'] = None if message.text.strip() == '-' else message.text.strip()
    await state.update_data(proxy_product_payload=payload)
    await state.set_state(AdminStates.creating_proxy_product_category)
    await message.answer('Введите ID категории ProxySoxy для этого товара.')


@admin_required
@error_handler
async def receive_proxy_product_category(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    payload = dict(data.get('proxy_product_payload') or {})
    payload['provider_category_id'] = message.text.strip()
    await state.update_data(proxy_product_payload=payload)
    await message.answer(
        'Выберите тип наценки:',
        reply_markup=_markup_type_keyboard('admin_proxy_create_markup_type', db_user.language),
    )


@admin_required
@error_handler
async def select_create_markup_type(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    markup_type = callback.data.split(':', 1)[1]
    data = await state.get_data()
    payload = dict(data.get('proxy_product_payload') or {})
    payload['markup_type'] = markup_type
    await state.update_data(proxy_product_payload=payload)
    await state.set_state(AdminStates.creating_proxy_product_markup_value)
    await callback.message.edit_text('Введите значение наценки (целое число).')
    await callback.answer()


@admin_required
@error_handler
async def receive_proxy_product_markup_value(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    value = int(message.text.strip())
    data = await state.get_data()
    payload = dict(data.get('proxy_product_payload') or {})
    payload['markup_value'] = max(0, value)
    await state.update_data(proxy_product_payload=payload)
    await state.set_state(AdminStates.creating_proxy_product_quantities)
    await message.answer('Введите диапазон количества: минимум и максимум. Например: `1 50`')


@admin_required
@error_handler
async def receive_proxy_product_quantities(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    minimum, maximum = _parse_quantity_range(message.text)
    data = await state.get_data()
    payload = dict(data.get('proxy_product_payload') or {})
    payload['min_quantity'] = minimum
    payload['max_quantity'] = maximum
    await state.update_data(proxy_product_payload=payload)
    await message.answer(
        'Выберите режим выдачи товара:',
        reply_markup=_source_mode_keyboard('admin_proxy_create_source_mode', db_user.language),
    )


@admin_required
@error_handler
async def finish_create_proxy_product(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    source_mode = callback.data.split(':', 1)[1]
    data = await state.get_data()
    payload = dict(data.get('proxy_product_payload') or {})
    payload['source_mode'] = source_mode
    product = await proxy_sales_service.upsert_product(db, **payload)
    await db.commit()
    await state.clear()
    stock_summary = await proxy_sales_service.get_stock_summary(db, product)
    await callback.message.edit_text(
        _product_text(product, stock_summary),
        parse_mode='HTML',
        reply_markup=_product_keyboard(product.id, db_user.language),
    )
    await callback.answer('Товар создан')


@admin_required
@error_handler
async def toggle_proxy_product(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    product_id = int(callback.data.split(':', 1)[1])
    product = await get_proxy_product_by_id(db, product_id)
    if product is None:
        await callback.answer('Товар не найден', show_alert=True)
        return
    product = await proxy_sales_service.upsert_product(db, product_id=product.id, is_active=not product.is_active)
    await db.commit()
    stock_summary = await proxy_sales_service.get_stock_summary(db, product)
    await callback.message.edit_text(
        _product_text(product, stock_summary),
        parse_mode='HTML',
        reply_markup=_product_keyboard(product.id, db_user.language),
    )
    await callback.answer('Статус обновлен')


@admin_required
@error_handler
async def delete_proxy_product_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    product_id = int(callback.data.split(':', 1)[1])
    product = await get_proxy_product_by_id(db, product_id)
    if product is None:
        await callback.answer('Товар не найден', show_alert=True)
        return
    await delete_proxy_product(db, product)
    await db.commit()
    await show_proxy_products(callback, db_user, db)


async def _start_edit_field(
    callback: types.CallbackQuery,
    state: FSMContext,
    product_id: int,
    field: str,
    state_name,
    prompt: str,
):
    await state.clear()
    await state.set_state(state_name)
    await state.update_data(proxy_product_id=product_id, proxy_edit_field=field)
    await callback.message.edit_text(prompt)
    await callback.answer()


@admin_required
@error_handler
async def edit_proxy_name(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    await _start_edit_field(
        callback,
        state,
        int(callback.data.split(':', 1)[1]),
        'name',
        AdminStates.editing_proxy_product_name,
        'Введите новое название товара.',
    )


@admin_required
@error_handler
async def edit_proxy_description(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    await _start_edit_field(
        callback,
        state,
        int(callback.data.split(':', 1)[1]),
        'description',
        AdminStates.editing_proxy_product_description,
        'Введите новое описание товара или `-` для очистки.',
    )


@admin_required
@error_handler
async def edit_proxy_category(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    await _start_edit_field(
        callback,
        state,
        int(callback.data.split(':', 1)[1]),
        'provider_category_id',
        AdminStates.editing_proxy_product_category,
        'Введите новый ID категории ProxySoxy.',
    )


@admin_required
@error_handler
async def edit_proxy_quantities(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    await _start_edit_field(
        callback,
        state,
        int(callback.data.split(':', 1)[1]),
        'quantity_range',
        AdminStates.editing_proxy_product_quantities,
        'Введите новый диапазон количества, например: `1 50`',
    )


@admin_required
@error_handler
async def edit_proxy_markup(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    product_id = int(callback.data.split(':', 1)[1])
    await state.clear()
    await state.update_data(proxy_product_id=product_id)
    await callback.message.edit_text(
        'Сначала выберите тип наценки:',
        reply_markup=_markup_type_keyboard('admin_proxy_edit_markup_type', db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_edit_markup_type(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    markup_type = callback.data.split(':', 1)[1]
    await state.update_data(proxy_markup_type=markup_type)
    await state.set_state(AdminStates.editing_proxy_product_markup_value)
    await callback.message.edit_text('Введите новое значение наценки.')
    await callback.answer()


@admin_required
@error_handler
async def edit_proxy_source(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    product_id = int(callback.data.split(':', 1)[1])
    await state.clear()
    await state.update_data(proxy_product_id=product_id)
    await callback.message.edit_text(
        'Выберите новый режим выдачи:',
        reply_markup=_source_mode_keyboard('admin_proxy_edit_source_mode', db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def finish_edit_source(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    source_mode = callback.data.split(':', 1)[1]
    data = await state.get_data()
    product = await proxy_sales_service.upsert_product(
        db,
        product_id=int(data['proxy_product_id']),
        source_mode=source_mode,
    )
    await db.commit()
    await state.clear()
    stock_summary = await proxy_sales_service.get_stock_summary(db, product)
    await callback.message.edit_text(
        _product_text(product, stock_summary),
        parse_mode='HTML',
        reply_markup=_product_keyboard(product.id, db_user.language),
    )
    await callback.answer('Режим обновлен')


@admin_required
@error_handler
async def save_edited_proxy_field(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    product_id = int(data['proxy_product_id'])
    field = data.get('proxy_edit_field')
    update_payload = {}
    if field == 'description':
        update_payload[field] = None if message.text.strip() == '-' else message.text.strip()
    elif field == 'quantity_range':
        minimum, maximum = _parse_quantity_range(message.text)
        update_payload['min_quantity'] = minimum
        update_payload['max_quantity'] = maximum
    else:
        update_payload[field] = message.text.strip()
    product = await proxy_sales_service.upsert_product(db, product_id=product_id, **update_payload)
    await db.commit()
    await state.clear()
    stock_summary = await proxy_sales_service.get_stock_summary(db, product)
    await message.answer(
        _product_text(product, stock_summary),
        parse_mode='HTML',
        reply_markup=_product_keyboard(product.id, db_user.language),
    )


@admin_required
@error_handler
async def save_edited_proxy_markup(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    product = await proxy_sales_service.upsert_product(
        db,
        product_id=int(data['proxy_product_id']),
        markup_type=data['proxy_markup_type'],
        markup_value=max(0, int(message.text.strip())),
    )
    await db.commit()
    await state.clear()
    stock_summary = await proxy_sales_service.get_stock_summary(db, product)
    await message.answer(
        _product_text(product, stock_summary),
        parse_mode='HTML',
        reply_markup=_product_keyboard(product.id, db_user.language),
    )


@admin_required
@error_handler
async def start_stock_purchase(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    product_id = int(callback.data.split(':', 1)[1])
    await state.clear()
    await state.set_state(AdminStates.proxy_stock_purchase_quantity)
    await state.update_data(proxy_product_id=product_id)
    await callback.message.edit_text('Введите количество для закупки на склад.')
    await callback.answer()


@admin_required
@error_handler
async def finish_stock_purchase(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    product_id = int(data['proxy_product_id'])
    quantity = int(message.text.strip())
    product = await get_proxy_product_by_id(db, product_id)
    if product is None:
        await message.answer('Товар не найден.')
        return
    try:
        _, items = await proxy_sales_service.buy_stock(db, product=product, quantity=quantity, bot=message.bot)
    except ProxySalesError as error:
        await message.answer(f'Не удалось закупить прокси: {html.escape(str(error))}', parse_mode='HTML')
        return
    await state.clear()
    stock_summary = await proxy_sales_service.get_stock_summary(db, product)
    await message.answer(
        f'✅ Закуплено <b>{len(items)}</b> прокси на склад.\n\n{_product_text(product, stock_summary)}',
        parse_mode='HTML',
        reply_markup=_product_keyboard(product.id, db_user.language),
    )


@admin_required
@error_handler
async def show_proxy_orders(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    orders = await list_proxy_orders(db, limit=30)
    await callback.message.edit_text(
        '📦 <b>Заказы прокси</b>\n\nВыберите заказ:',
        parse_mode='HTML',
        reply_markup=_orders_keyboard(orders, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_proxy_order(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    order_id = int(callback.data.split(':', 1)[1])
    order = await get_proxy_order_by_id(db, order_id)
    if order is None:
        await callback.answer('Заказ не найден', show_alert=True)
        return
    product_name = order.product.name if order.product else f'Товар #{order.product_id or "?"}'
    lines = [
        f'📦 <b>Заказ #{order.id}</b>',
        '',
        f'Пользователь: <code>{order.user.telegram_id if order.user else order.user_id}</code>',
        f'Товар: <b>{html.escape(product_name)}</b>',
        f'Статус: <b>{html.escape(order.status)}</b>',
        f'Количество: <b>{order.quantity}</b>',
        f'Сумма: <b>{get_texts(db_user.language).format_price(order.total_price_kopeks)}</b>',
        '',
        '<b>Выданные прокси:</b>',
    ]
    for item in order.items:
        if item.stock_item is None:
            continue
        lines.append(f'• #{item.id}: <code>{html.escape(item.stock_item.get_delivery_line())}</code>')
    await callback.message.edit_text(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=_order_keyboard(order, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def replace_proxy_order_item(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    order_item_id = int(callback.data.split(':', 1)[1])
    try:
        order_item = await proxy_sales_service.replace_order_item(db, order_item_id=order_item_id, bot=callback.bot)
    except ProxySalesError as error:
        await callback.answer(str(error), show_alert=True)
        return
    order = await get_proxy_order_by_id(db, order_item.order_id)
    if order is not None:
        product_name = order.product.name if order.product else f'Товар #{order.product_id or "?"}'
        lines = [
            f'📦 <b>Заказ #{order.id}</b>',
            '',
            f'Пользователь: <code>{order.user.telegram_id if order.user else order.user_id}</code>',
            f'Товар: <b>{html.escape(product_name)}</b>',
            f'Статус: <b>{html.escape(order.status)}</b>',
            f'Количество: <b>{order.quantity}</b>',
            f'Сумма: <b>{get_texts(db_user.language).format_price(order.total_price_kopeks)}</b>',
            '',
            '<b>Выданные прокси:</b>',
        ]
        for item in order.items:
            if item.stock_item is None:
                continue
            lines.append(f'• #{item.id}: <code>{html.escape(item.stock_item.get_delivery_line())}</code>')
        await callback.message.edit_text(
            '\n'.join(lines),
            parse_mode='HTML',
            reply_markup=_order_keyboard(order, db_user.language),
        )
    await callback.answer(f'Замена выполнена: #{order_item.id}')


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_proxy_sales_menu, F.data == 'admin_proxy_sales')
    dp.callback_query.register(toggle_proxy_sales, F.data == 'admin_proxy_toggle_sales')
    dp.callback_query.register(show_proxy_status, F.data == 'admin_proxy_status')
    dp.callback_query.register(show_proxy_products, F.data == 'admin_proxy_products')
    dp.callback_query.register(show_proxy_product, F.data.startswith('admin_proxy_product:'))
    dp.callback_query.register(start_create_proxy_product, F.data == 'admin_proxy_product_new')
    dp.callback_query.register(select_create_markup_type, F.data.startswith('admin_proxy_create_markup_type:'))
    dp.callback_query.register(finish_create_proxy_product, F.data.startswith('admin_proxy_create_source_mode:'))
    dp.callback_query.register(toggle_proxy_product, F.data.startswith('admin_proxy_toggle:'))
    dp.callback_query.register(delete_proxy_product_handler, F.data.startswith('admin_proxy_delete:'))
    dp.callback_query.register(edit_proxy_name, F.data.startswith('admin_proxy_edit_name:'))
    dp.callback_query.register(edit_proxy_description, F.data.startswith('admin_proxy_edit_description:'))
    dp.callback_query.register(edit_proxy_category, F.data.startswith('admin_proxy_edit_category:'))
    dp.callback_query.register(edit_proxy_markup, F.data.startswith('admin_proxy_edit_markup:'))
    dp.callback_query.register(edit_proxy_quantities, F.data.startswith('admin_proxy_edit_quantities:'))
    dp.callback_query.register(edit_proxy_source, F.data.startswith('admin_proxy_edit_source:'))
    dp.callback_query.register(select_edit_markup_type, F.data.startswith('admin_proxy_edit_markup_type:'))
    dp.callback_query.register(finish_edit_source, F.data.startswith('admin_proxy_edit_source_mode:'))
    dp.callback_query.register(start_stock_purchase, F.data.startswith('admin_proxy_stock_buy:'))
    dp.callback_query.register(show_proxy_orders, F.data == 'admin_proxy_orders')
    dp.callback_query.register(show_proxy_order, F.data.startswith('admin_proxy_order:'))
    dp.callback_query.register(replace_proxy_order_item, F.data.startswith('admin_proxy_replace_item:'))

    dp.message.register(
        receive_proxy_product_name,
        StateFilter(AdminStates.creating_proxy_product_name),
    )
    dp.message.register(
        receive_proxy_product_description,
        StateFilter(AdminStates.creating_proxy_product_description),
    )
    dp.message.register(
        receive_proxy_product_category,
        StateFilter(AdminStates.creating_proxy_product_category),
    )
    dp.message.register(
        receive_proxy_product_markup_value,
        StateFilter(AdminStates.creating_proxy_product_markup_value),
    )
    dp.message.register(
        receive_proxy_product_quantities,
        StateFilter(AdminStates.creating_proxy_product_quantities),
    )
    dp.message.register(
        save_edited_proxy_field,
        StateFilter(
            AdminStates.editing_proxy_product_name,
            AdminStates.editing_proxy_product_description,
            AdminStates.editing_proxy_product_category,
            AdminStates.editing_proxy_product_quantities,
        ),
    )
    dp.message.register(
        save_edited_proxy_markup,
        StateFilter(AdminStates.editing_proxy_product_markup_value),
    )
    dp.message.register(
        finish_stock_purchase,
        StateFilter(AdminStates.proxy_stock_purchase_quantity),
    )
