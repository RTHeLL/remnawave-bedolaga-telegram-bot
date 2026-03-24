from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)
PROXYSOXY_BASE_URL = 'https://proxysoxy.com'
PROXYSOXY_AUTH_PATH = '/api/api-auth'
PROXYSOXY_CATEGORIES_PATH = '/api/categories'
PROXYSOXY_USER_INFO_PATH = '/api/me'
PROXYSOXY_ORDERS_PATH = '/api/orders'
PROXYSOXY_ORDER_DOWNLOAD_PATH_TEMPLATE = '/api/orders/{order_id}/download'


class ProxySoxyAPIError(Exception):
    """Base error for ProxySoxy API operations."""


@dataclass(slots=True)
class ProxySoxyCategory:
    id: str
    name: str
    unit_cost_kopeks: int
    currency: str = 'RUB'
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class ProxySoxyProxyItem:
    endpoint: str | None
    host: str | None
    port: int | None
    username: str | None
    password: str | None
    protocol: str | None
    country: str | None
    provider_item_id: str | None
    expires_at: datetime | None
    raw: dict[str, Any] | None


@dataclass(slots=True)
class ProxySoxyOrderResult:
    order_id: str | None
    quantity: int
    unit_cost_kopeks: int
    total_cost_kopeks: int
    currency: str
    items: list[ProxySoxyProxyItem]
    raw: dict[str, Any] | None = None


class ProxySoxyClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or PROXYSOXY_BASE_URL).rstrip('/')
        self.api_key = api_key or settings.PROXYSOXY_API_KEY
        self.timeout = float(timeout or settings.PROXYSOXY_TIMEOUT or 30.0)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    @property
    def is_configured(self) -> bool:
        return bool(settings.is_proxysoxy_configured() and self.api_key and self.base_url)

    async def authenticate(self, *, force: bool = False) -> str:
        if not self.is_configured:
            raise ProxySoxyAPIError('ProxySoxy API key is not configured')

        if not force and self._token and self._token_expires_at and self._token_expires_at > datetime.now(UTC):
            return self._token

        payload = {'authToken': self.api_key}
        data = await self._raw_request('POST', PROXYSOXY_AUTH_PATH, json_payload=payload, require_auth=False)

        token = (
            data.get('token')
            or data.get('accessToken')
            or data.get('access_token')
            or data.get('jwt')
            or data.get('data', {}).get('token')
        )
        if not token:
            raise ProxySoxyAPIError(f'ProxySoxy auth response does not contain token: {data}')

        self._token = str(token)
        expires_in_raw = data.get('expiresIn') or data.get('expires_in') or data.get('ttl')
        try:
            expires_in = int(expires_in_raw) if expires_in_raw is not None else 1800
        except (TypeError, ValueError):
            expires_in = 1800
        self._token_expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=max(60, expires_in - 30))
        return self._token

    async def get_user_info(self) -> dict[str, Any]:
        return await self._raw_request('GET', PROXYSOXY_USER_INFO_PATH)

    async def get_balance(self) -> tuple[int, str, dict[str, Any]]:
        payload = await self.get_user_info()
        balance_value = (
            payload.get('balance')
            or payload.get('amount')
            or payload.get('data', {}).get('balance')
            or payload.get('data', {}).get('amount')
        )
        currency = (
            payload.get('currency')
            or payload.get('data', {}).get('currency')
            or payload.get('wallet', {}).get('currency')
            or 'RUB'
        )
        return _to_kopeks(balance_value), str(currency or 'RUB'), payload

    async def get_categories(self) -> list[ProxySoxyCategory]:
        payload = await self._raw_request('GET', PROXYSOXY_CATEGORIES_PATH)
        categories_raw = payload.get('data') if isinstance(payload.get('data'), list) else payload
        if isinstance(categories_raw, dict):
            categories_raw = categories_raw.get('categories') or categories_raw.get('items') or []
        if not isinstance(categories_raw, list):
            raise ProxySoxyAPIError(f'Unexpected categories payload: {payload}')

        categories: list[ProxySoxyCategory] = []
        for item in categories_raw:
            if not isinstance(item, dict):
                continue
            category_id = item.get('id') or item.get('_id') or item.get('uuid') or item.get('slug')
            if category_id is None:
                continue
            name = item.get('name') or item.get('title') or f'Category {category_id}'
            price = (
                item.get('price')
                or item.get('cost')
                or item.get('amount')
                or item.get('price_rub')
                or item.get('data', {}).get('price')
            )
            currency = item.get('currency') or 'RUB'
            categories.append(
                ProxySoxyCategory(
                    id=str(category_id),
                    name=str(name),
                    unit_cost_kopeks=_to_kopeks(price),
                    currency=str(currency or 'RUB'),
                    raw=item,
                )
            )
        return categories

    async def get_category(self, category_id: str) -> ProxySoxyCategory:
        categories = await self.get_categories()
        for category in categories:
            if str(category.id) == str(category_id):
                return category
        raise ProxySoxyAPIError(f'ProxySoxy category not found: {category_id}')

    async def create_order(
        self,
        *,
        category_id: str,
        quantity: int,
        extra_payload: dict[str, Any] | None = None,
    ) -> ProxySoxyOrderResult:
        payload: dict[str, Any] = {
            'category_id': category_id,
            'quantity': max(1, int(quantity)),
        }
        if extra_payload:
            payload.update(extra_payload)

        response = await self._raw_request('POST', PROXYSOXY_ORDERS_PATH, json_payload=payload)
        return self._parse_order_result(response)

    async def get_order(self, order_id: str) -> ProxySoxyOrderResult:
        payload = await self._raw_request('GET', f'{PROXYSOXY_ORDERS_PATH.rstrip("/")}/{order_id}')
        return self._parse_order_result(payload)

    async def list_orders(self) -> list[dict[str, Any]]:
        payload = await self._raw_request('GET', PROXYSOXY_ORDERS_PATH)
        data = payload.get('data') if isinstance(payload.get('data'), list) else payload
        if isinstance(data, dict):
            data = data.get('orders') or data.get('items') or []
        return data if isinstance(data, list) else []

    async def download_order(self, order_id: str) -> list[ProxySoxyProxyItem]:
        path = PROXYSOXY_ORDER_DOWNLOAD_PATH_TEMPLATE.format(order_id=order_id)
        text_payload = await self._raw_request('GET', path, return_text=True)
        return _parse_download_text(text_payload)

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        require_auth: bool = True,
        return_text: bool = False,
        retry_on_auth_error: bool = True,
    ) -> Any:
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
        }
        if require_auth:
            token = await self.authenticate()
            headers['Authorization'] = f'Bearer {token}'

        url = f'{self.base_url}/{path.lstrip("/")}'
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(method, url, headers=headers, params=params, json=json_payload) as response,
            ):
                if return_text:
                    body = await response.text()
                    if response.status >= 400:
                        raise ProxySoxyAPIError(f'ProxySoxy error {response.status} at {path}: {body}')
                    return body

                try:
                    payload = await response.json(content_type=None)
                except aiohttp.ContentTypeError:
                    raw_text = await response.text()
                    raise ProxySoxyAPIError(
                        f'ProxySoxy returned non-JSON response for {path}: status={response.status}, body={raw_text}'
                    ) from None

                if response.status in {401, 403} and require_auth:
                    self._token = None
                    self._token_expires_at = None
                    if retry_on_auth_error:
                        await self.authenticate(force=True)
                        return await self._raw_request(
                            method,
                            path,
                            params=params,
                            json_payload=json_payload,
                            require_auth=True,
                            return_text=return_text,
                            retry_on_auth_error=False,
                        )

                if response.status >= 400:
                    raise ProxySoxyAPIError(
                        f'ProxySoxy error {response.status} at {path}: {payload.get("message") or payload.get("error") or payload}'
                    )

                return payload
        except TimeoutError as error:
            logger.error('ProxySoxy request timeout', method=method, path=path, error=error)
            raise ProxySoxyAPIError(f'ProxySoxy request timeout for {path}') from error
        except aiohttp.ClientError as error:
            logger.error('ProxySoxy request failed', method=method, path=path, error=error)
            raise ProxySoxyAPIError(str(error)) from error

    def _parse_order_result(self, payload: dict[str, Any]) -> ProxySoxyOrderResult:
        data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
        order_id = data.get('id') or data.get('order_id') or data.get('uuid')
        quantity = int(data.get('quantity') or data.get('count') or len(data.get('items') or data.get('proxies') or []))
        unit_cost_kopeks = _to_kopeks(
            data.get('unit_price') or data.get('unit_cost') or data.get('price') or data.get('price_per_item')
        )
        total_cost_kopeks = _to_kopeks(data.get('total') or data.get('total_price') or data.get('amount'))
        currency = str(data.get('currency') or 'RUB')
        items_raw = data.get('items') or data.get('proxies') or []
        items = [_parse_proxy_item(item) for item in items_raw if isinstance(item, dict)]

        if (not items) and order_id:
            try:
                items = []
            except Exception:
                items = []

        return ProxySoxyOrderResult(
            order_id=str(order_id) if order_id is not None else None,
            quantity=max(0, quantity),
            unit_cost_kopeks=unit_cost_kopeks,
            total_cost_kopeks=total_cost_kopeks if total_cost_kopeks > 0 else unit_cost_kopeks * max(0, quantity),
            currency=currency,
            items=items,
            raw=payload,
        )


def _to_kopeks(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value >= 1000 else value * 100
    try:
        normalized = str(value).replace(',', '.').strip()
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return 0
    return int((amount * 100).quantize(Decimal('1')))


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_proxy_item(payload: dict[str, Any]) -> ProxySoxyProxyItem:
    host = payload.get('host') or payload.get('ip')
    port_raw = payload.get('port')
    try:
        port = int(port_raw) if port_raw is not None else None
    except (TypeError, ValueError):
        port = None
    endpoint = payload.get('endpoint') or payload.get('proxy')
    if not endpoint and host and port:
        endpoint = f'{host}:{port}'
    return ProxySoxyProxyItem(
        endpoint=endpoint,
        host=host,
        port=port,
        username=payload.get('username') or payload.get('login'),
        password=payload.get('password') or payload.get('pass'),
        protocol=payload.get('protocol') or payload.get('type'),
        country=payload.get('country'),
        provider_item_id=str(payload.get('id')) if payload.get('id') is not None else None,
        expires_at=_parse_datetime(payload.get('expires_at') or payload.get('expire_at')),
        raw=payload,
    )


def _parse_download_text(payload: str) -> list[ProxySoxyProxyItem]:
    items: list[ProxySoxyProxyItem] = []
    for line in payload.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        protocol = None
        endpoint = stripped
        if '://' in stripped:
            protocol, endpoint = stripped.split('://', 1)
        credentials = None
        host_port = endpoint
        if '@' in endpoint:
            credentials, host_port = endpoint.split('@', 1)
        host = None
        port = None
        if ':' in host_port:
            host, port_raw = host_port.rsplit(':', 1)
            try:
                port = int(port_raw)
            except ValueError:
                port = None
        username = None
        password = None
        if credentials and ':' in credentials:
            username, password = credentials.split(':', 1)
        items.append(
            ProxySoxyProxyItem(
                endpoint=stripped,
                host=host,
                port=port,
                username=username,
                password=password,
                protocol=protocol,
                country=None,
                provider_item_id=None,
                expires_at=None,
                raw=None,
            )
        )
    return items
