"""pricing: configurable plans, rates, billing settings and price history

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-10 10:00:00.000000

"""
from typing import Sequence, Union
from datetime import datetime, date
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Tabla pricing_plans
    op.create_table(
        'pricing_plans',
        sa.Column('code', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('discount_rate', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('code'),
    )

    # 1b. Tabla billing_settings
    op.create_table(
        'billing_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('grace_days', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by_admin_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['updated_by_admin_id'], ['users.id'], name='fk_billing_settings_admin_id_users'),
        sa.PrimaryKeyConstraint('id'),
    )

    # 2. Tabla pricing_rates
    op.create_table(
        'pricing_rates',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('income_source', sa.String(length=20), nullable=False),
        sa.Column('taxpayer_type', sa.String(length=20), nullable=False),
        sa.Column('monthly_price', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('created_by_admin_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('monthly_price > 0', name='ck_pricing_rates_monthly_price_positive'),
        sa.ForeignKeyConstraint(['created_by_admin_id'], ['users.id'], name='fk_pricing_rates_admin_id_users'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('income_source', 'taxpayer_type', 'effective_from', name='uq_pricing_rates_segment_effective'),
    )
    op.create_index(
        'idx_pricing_rates_lookup',
        'pricing_rates',
        ['income_source', 'taxpayer_type', 'effective_from'],
        unique=False,
    )

    # 3. Columnas nuevas en subscriptions
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('monthly_price', sa.Numeric(precision=14, scale=2), nullable=True))
        batch_op.add_column(sa.Column('price_origin', sa.String(length=20), server_default='TARIFA', nullable=False))
        batch_op.add_column(sa.Column('price_note', sa.Text(), nullable=True))

    # 4. Tabla subscription_price_changes
    op.create_table(
        'subscription_price_changes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('subscription_id', sa.String(length=36), nullable=False),
        sa.Column('old_plan', sa.String(length=20), nullable=True),
        sa.Column('new_plan', sa.String(length=20), nullable=True),
        sa.Column('old_monthly_price', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('new_monthly_price', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('old_discount_rate', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('new_discount_rate', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('old_final_price', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('new_final_price', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('admin_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['admin_id'], ['users.id'], name='fk_subscription_price_changes_admin_id_users'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE', name='fk_sub_price_changes_sub_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_price_changes_subscription',
        'subscription_price_changes',
        ['subscription_id'],
        unique=False,
    )

    # 5. Columna nueva en payment_records
    with op.batch_alter_table('payment_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expected_amount', sa.Numeric(precision=14, scale=2), nullable=True))

    # Semilla en pricing_plans con bulk_insert
    plans_table = table(
        'pricing_plans',
        column('code', sa.String),
        column('name', sa.String),
        column('months', sa.Integer),
        column('discount_rate', sa.Numeric),
        column('is_active', sa.Boolean),
        column('sort_order', sa.Integer),
        column('created_at', sa.DateTime),
        column('updated_at', sa.DateTime),
    )
    now_dt = datetime(2026, 1, 1, 0, 0, 0)
    op.bulk_insert(
        plans_table,
        [
            {
                'code': 'MENSUAL',
                'name': 'Mensual',
                'months': 1,
                'discount_rate': 0.00,
                'is_active': False,
                'sort_order': 1,
                'created_at': now_dt,
                'updated_at': now_dt,
            },
            {
                'code': 'TRIMESTRAL',
                'name': 'Trimestral',
                'months': 3,
                'discount_rate': 5.00,
                'is_active': True,
                'sort_order': 2,
                'created_at': now_dt,
                'updated_at': now_dt,
            },
            {
                'code': 'SEMESTRAL',
                'name': 'Semestral',
                'months': 6,
                'discount_rate': 8.00,
                'is_active': True,
                'sort_order': 3,
                'created_at': now_dt,
                'updated_at': now_dt,
            },
            {
                'code': 'ANUAL',
                'name': 'Anual',
                'months': 12,
                'discount_rate': 10.00,
                'is_active': True,
                'sort_order': 4,
                'created_at': now_dt,
                'updated_at': now_dt,
            },
        ],
    )

    # Semilla en billing_settings
    billing_table = table(
        'billing_settings',
        column('id', sa.Integer),
        column('grace_days', sa.Integer),
        column('updated_at', sa.DateTime),
        column('updated_by_admin_id', sa.String),
    )
    op.bulk_insert(
        billing_table,
        [
            {
                'id': 1,
                'grace_days': 3,
                'updated_at': now_dt,
                'updated_by_admin_id': None,
            }
        ],
    )

    # Semilla en pricing_rates
    rates_table = table(
        'pricing_rates',
        column('id', sa.String),
        column('income_source', sa.String),
        column('taxpayer_type', sa.String),
        column('monthly_price', sa.Numeric),
        column('effective_from', sa.Date),
        column('created_by_admin_id', sa.String),
        column('created_at', sa.DateTime),
    )
    eff_date = date(2026, 1, 1)
    op.bulk_insert(
        rates_table,
        [
            {
                'id': 'rate-dian-persona-natural',
                'income_source': 'DIAN',
                'taxpayer_type': 'PERSONA_NATURAL',
                'monthly_price': 50000.00,
                'effective_from': eff_date,
                'created_by_admin_id': None,
                'created_at': now_dt,
            },
            {
                'id': 'rate-dian-persona-juridica',
                'income_source': 'DIAN',
                'taxpayer_type': 'PERSONA_JURIDICA',
                'monthly_price': 50000.00,
                'effective_from': eff_date,
                'created_by_admin_id': None,
                'created_at': now_dt,
            },
            {
                'id': 'rate-manual-persona-natural',
                'income_source': 'MANUAL_SALES',
                'taxpayer_type': 'PERSONA_NATURAL',
                'monthly_price': 50000.00,
                'effective_from': eff_date,
                'created_by_admin_id': None,
                'created_at': now_dt,
            },
            {
                'id': 'rate-manual-persona-juridica',
                'income_source': 'MANUAL_SALES',
                'taxpayer_type': 'PERSONA_JURIDICA',
                'monthly_price': 50000.00,
                'effective_from': eff_date,
                'created_by_admin_id': None,
                'created_at': now_dt,
            },
        ],
    )

    # Relleno de subscriptions.monthly_price compatible con SQLite y PostgreSQL
    op.execute(
        """
        UPDATE subscriptions
        SET monthly_price = ROUND(
            base_price / CASE plan
                WHEN 'MENSUAL' THEN 1
                WHEN 'TRIMESTRAL' THEN 3
                WHEN 'SEMESTRAL' THEN 6
                WHEN 'ANUAL' THEN 12
                ELSE 3
            END,
            2
        )
        WHERE monthly_price IS NULL
        """
    )


def downgrade() -> None:
    # 1. Quitar columna de payment_records
    with op.batch_alter_table('payment_records', schema=None) as batch_op:
        batch_op.drop_column('expected_amount')

    # 2. Eliminar subscription_price_changes
    op.drop_index('idx_price_changes_subscription', table_name='subscription_price_changes')
    op.drop_table('subscription_price_changes')

    # 3. Quitar columnas de subscriptions
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('price_note')
        batch_op.drop_column('price_origin')
        batch_op.drop_column('monthly_price')

    # 4. Eliminar pricing_rates
    op.drop_index('idx_pricing_rates_lookup', table_name='pricing_rates')
    op.drop_table('pricing_rates')

    # 5. Eliminar billing_settings
    op.drop_table('billing_settings')

    # 6. Eliminar pricing_plans
    op.drop_table('pricing_plans')
