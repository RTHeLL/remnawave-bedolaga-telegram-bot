"""Schemas for proxy sales cabinet endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProxyProductResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    provider_category_id: str
    provider_category_name: str | None = None
    source_mode: str
    markup_type: str
    markup_value: int
    min_quantity: int
    max_quantity: int
    is_active: bool
    is_visible_in_catalog: bool
    in_stock_count: int
    unit_price_kopeks: int | None = None
    currency: str | None = None


class ProxyProductListResponse(BaseModel):
    sales_enabled: bool
    items: list[ProxyProductResponse]


class ProxyPurchasePreviewRequest(BaseModel):
    quantity: int = Field(..., ge=1)


class ProxyPurchasePreviewResponse(BaseModel):
    product_id: int
    quantity: int
    unit_price_kopeks: int
    total_price_kopeks: int
    currency: str
    balance_kopeks: int
    balance_after_purchase_kopeks: int


class ProxyOrderItemResponse(BaseModel):
    id: int
    delivery_line: str
    is_replacement: bool
    delivered_at: datetime | None = None


class ProxyOrderResponse(BaseModel):
    id: int
    product_id: int | None = None
    product_name: str
    status: str
    quantity: int
    delivered_quantity: int
    unit_price_kopeks: int
    total_price_kopeks: int
    total_cost_kopeks: int
    currency: str
    created_at: datetime
    paid_at: datetime | None = None
    fulfilled_at: datetime | None = None
    error_message: str | None = None
    delivery_lines: list[str]
    items: list[ProxyOrderItemResponse]

    model_config = ConfigDict(from_attributes=True)


class ProxyOrderListResponse(BaseModel):
    items: list[ProxyOrderResponse]


class ProxyPurchaseResponse(BaseModel):
    order: ProxyOrderResponse
    balance_kopeks: int
    charged_amount_kopeks: int
