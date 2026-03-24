"""add proxy sales tables

Revision ID: 0050
Revises: 0049
Create Date: 2026-03-24

Adds a dedicated schema for selling proxy products:
- proxy_products
- proxy_provider_purchases
- proxy_orders
- proxy_stock_items
- proxy_order_items
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0050'
down_revision: str | None = '0049'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'proxy_products',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('provider_name', sa.String(length=50), nullable=False, server_default='proxysoxy'),
        sa.Column('provider_category_id', sa.String(length=255), nullable=False),
        sa.Column('provider_category_name', sa.String(length=255), nullable=True),
        sa.Column('source_mode', sa.String(length=32), nullable=False, server_default='stock_first'),
        sa.Column('markup_type', sa.String(length=32), nullable=False, server_default='fixed'),
        sa.Column('markup_value', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('min_quantity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('max_quantity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('is_visible_in_catalog', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_proxy_products_active_order', 'proxy_products', ['is_active', 'display_order'])
    op.create_index('ix_proxy_products_category_active', 'proxy_products', ['provider_category_id', 'is_active'])
    op.create_index('ix_proxy_products_provider_category_id', 'proxy_products', ['provider_category_id'])

    op.create_table(
        'proxy_provider_purchases',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('purchase_type', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('provider_name', sa.String(length=50), nullable=False, server_default='proxysoxy'),
        sa.Column('provider_order_id', sa.String(length=255), nullable=True),
        sa.Column('requested_quantity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('fulfilled_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('unit_cost_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_cost_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(length=16), nullable=False, server_default='RUB'),
        sa.Column('request_payload', sa.JSON(), nullable=True),
        sa.Column('response_payload', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['product_id'], ['proxy_products.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    )
    op.create_index(
        'ix_proxy_provider_purchases_status_created',
        'proxy_provider_purchases',
        ['status', 'created_at'],
    )
    op.create_index(
        'ix_proxy_provider_purchases_type_created',
        'proxy_provider_purchases',
        ['purchase_type', 'created_at'],
    )
    op.create_index('ix_proxy_provider_purchases_provider_order_id', 'proxy_provider_purchases', ['provider_order_id'])
    op.create_index('ix_proxy_provider_purchases_product_id', 'proxy_provider_purchases', ['product_id'])
    op.create_index('ix_proxy_provider_purchases_user_id', 'proxy_provider_purchases', ['user_id'])

    op.create_table(
        'proxy_orders',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('provider_purchase_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('unit_price_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_price_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_cost_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(length=16), nullable=False, server_default='RUB'),
        sa.Column('source_mode', sa.String(length=32), nullable=False, server_default='stock_first'),
        sa.Column('delivered_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('delivery_payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fulfilled_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['product_id'], ['proxy_products.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['provider_purchase_id'], ['proxy_provider_purchases.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_proxy_orders_user_created', 'proxy_orders', ['user_id', 'created_at'])
    op.create_index('ix_proxy_orders_status_created', 'proxy_orders', ['status', 'created_at'])
    op.create_index('ix_proxy_orders_product_created', 'proxy_orders', ['product_id', 'created_at'])
    op.create_index('ix_proxy_orders_transaction_id', 'proxy_orders', ['transaction_id'])
    op.create_index('ix_proxy_orders_provider_purchase_id', 'proxy_orders', ['provider_purchase_id'])

    op.create_table(
        'proxy_stock_items',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('provider_purchase_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='in_stock'),
        sa.Column('provider_item_id', sa.String(length=255), nullable=True),
        sa.Column('provider_order_id', sa.String(length=255), nullable=True),
        sa.Column('reserved_for_order_id', sa.Integer(), nullable=True),
        sa.Column('unit_cost_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(length=16), nullable=False, server_default='RUB'),
        sa.Column('endpoint', sa.String(length=255), nullable=True),
        sa.Column('host', sa.String(length=255), nullable=True),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('password', sa.String(length=255), nullable=True),
        sa.Column('protocol', sa.String(length=32), nullable=True),
        sa.Column('country', sa.String(length=64), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('sold_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['product_id'], ['proxy_products.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['provider_purchase_id'], ['proxy_provider_purchases.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['reserved_for_order_id'], ['proxy_orders.id'], ondelete='SET NULL'),
    )
    op.create_index('ix_proxy_stock_items_status_product', 'proxy_stock_items', ['status', 'product_id'])
    op.create_index('ix_proxy_stock_items_reserved_order', 'proxy_stock_items', ['reserved_for_order_id'])
    op.create_index('ix_proxy_stock_items_provider_item_id', 'proxy_stock_items', ['provider_item_id'])
    op.create_index('ix_proxy_stock_items_provider_order_id', 'proxy_stock_items', ['provider_order_id'])
    op.create_index('ix_proxy_stock_items_product_id', 'proxy_stock_items', ['product_id'])
    op.create_index('ix_proxy_stock_items_provider_purchase_id', 'proxy_stock_items', ['provider_purchase_id'])

    op.create_table(
        'proxy_order_items',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('stock_item_id', sa.Integer(), nullable=False),
        sa.Column('unit_price_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('unit_cost_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_replacement', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('replaced_order_item_id', sa.Integer(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['order_id'], ['proxy_orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['replaced_order_item_id'], ['proxy_order_items.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['stock_item_id'], ['proxy_stock_items.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('order_id', 'stock_item_id', name='uq_proxy_order_items_order_stock'),
    )
    op.create_index('ix_proxy_order_items_order', 'proxy_order_items', ['order_id'])
    op.create_index('ix_proxy_order_items_stock', 'proxy_order_items', ['stock_item_id'])


def downgrade() -> None:
    op.drop_index('ix_proxy_order_items_stock', table_name='proxy_order_items')
    op.drop_index('ix_proxy_order_items_order', table_name='proxy_order_items')
    op.drop_table('proxy_order_items')

    op.drop_index('ix_proxy_stock_items_provider_purchase_id', table_name='proxy_stock_items')
    op.drop_index('ix_proxy_stock_items_product_id', table_name='proxy_stock_items')
    op.drop_index('ix_proxy_stock_items_provider_order_id', table_name='proxy_stock_items')
    op.drop_index('ix_proxy_stock_items_provider_item_id', table_name='proxy_stock_items')
    op.drop_index('ix_proxy_stock_items_reserved_order', table_name='proxy_stock_items')
    op.drop_index('ix_proxy_stock_items_status_product', table_name='proxy_stock_items')
    op.drop_table('proxy_stock_items')

    op.drop_index('ix_proxy_orders_provider_purchase_id', table_name='proxy_orders')
    op.drop_index('ix_proxy_orders_transaction_id', table_name='proxy_orders')
    op.drop_index('ix_proxy_orders_product_created', table_name='proxy_orders')
    op.drop_index('ix_proxy_orders_status_created', table_name='proxy_orders')
    op.drop_index('ix_proxy_orders_user_created', table_name='proxy_orders')
    op.drop_table('proxy_orders')

    op.drop_index('ix_proxy_provider_purchases_user_id', table_name='proxy_provider_purchases')
    op.drop_index('ix_proxy_provider_purchases_product_id', table_name='proxy_provider_purchases')
    op.drop_index('ix_proxy_provider_purchases_provider_order_id', table_name='proxy_provider_purchases')
    op.drop_index('ix_proxy_provider_purchases_type_created', table_name='proxy_provider_purchases')
    op.drop_index('ix_proxy_provider_purchases_status_created', table_name='proxy_provider_purchases')
    op.drop_table('proxy_provider_purchases')

    op.drop_index('ix_proxy_products_provider_category_id', table_name='proxy_products')
    op.drop_index('ix_proxy_products_category_active', table_name='proxy_products')
    op.drop_index('ix_proxy_products_active_order', table_name='proxy_products')
    op.drop_table('proxy_products')
