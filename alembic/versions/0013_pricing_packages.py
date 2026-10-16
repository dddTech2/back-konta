"""pricing_packages: tariffs by package (CON_IVA, SIN_IVA), backfill subscriptions

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15 14:00:00.000000

"""
from typing import Sequence, Union
from datetime import datetime, date
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Modificar tabla pricing_rates:
    # - Agregar columna package String(10) NULL
    # - Columnas income_source y taxpayer_type pasan a nullable
    # - Eliminar restricción única uq_pricing_rates_segment_effective e índice idx_pricing_rates_lookup
    # - Crear nueva restricción única uq_pricing_rates_package_effective e índice idx_pricing_rates_package
    with op.batch_alter_table('pricing_rates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('package', sa.String(length=10), nullable=True))
        batch_op.alter_column('income_source', existing_type=sa.String(length=20), nullable=True)
        batch_op.alter_column('taxpayer_type', existing_type=sa.String(length=20), nullable=True)
        batch_op.drop_constraint('uq_pricing_rates_segment_effective', type_='unique')
        batch_op.drop_index('idx_pricing_rates_lookup')
        batch_op.create_unique_constraint('uq_pricing_rates_package_effective', ['package', 'effective_from'])
        batch_op.create_index('idx_pricing_rates_package', ['package', 'effective_from'], unique=False)

    # 2. Semilla de tarifas por paquete: CON_IVA $349.900 y SIN_IVA $249.900 vigentes desde literal 2026-10-15
    rates_table = table(
        'pricing_rates',
        column('id', sa.String),
        column('package', sa.String),
        column('income_source', sa.String),
        column('taxpayer_type', sa.String),
        column('monthly_price', sa.Numeric),
        column('effective_from', sa.Date),
        column('created_by_admin_id', sa.String),
        column('created_at', sa.DateTime),
    )
    eff_date = date(2026, 10, 15)
    now_dt = datetime(2026, 10, 15, 0, 0, 0)
    op.bulk_insert(
        rates_table,
        [
            {
                'id': 'rate-con-iva',
                'package': 'CON_IVA',
                'income_source': None,
                'taxpayer_type': None,
                'monthly_price': 349900.00,
                'effective_from': eff_date,
                'created_by_admin_id': None,
                'created_at': now_dt,
            },
            {
                'id': 'rate-sin-iva',
                'package': 'SIN_IVA',
                'income_source': None,
                'taxpayer_type': None,
                'monthly_price': 249900.00,
                'effective_from': eff_date,
                'created_by_admin_id': None,
                'created_at': now_dt,
            },
        ],
    )

    # 3. Agregar columna package a subscriptions y rellenar desde el negocio activo más antiguo del dueño
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('package', sa.String(length=10), nullable=True))

    op.execute(
        """
        UPDATE subscriptions
        SET package = COALESCE(
            (
                SELECT CASE
                    WHEN b.vat_responsible THEN 'CON_IVA'
                    ELSE 'SIN_IVA'
                END
                FROM businesses b
                WHERE b.client_id = subscriptions.client_id
                  AND b.is_active
                ORDER BY b.created_at ASC
                LIMIT 1
            ),
            'SIN_IVA'
        )
        WHERE subscriptions.package IS NULL
        """
    )


def downgrade() -> None:
    # 1. Borrar filas de pricing_rates con package no nulo
    op.execute("DELETE FROM pricing_rates WHERE package IS NOT NULL")

    # 2. Quitar columna package de subscriptions
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('package')

    # 3. Revertir pricing_rates: restaurar columnas NOT NULL, restricción única e índice anteriores
    with op.batch_alter_table('pricing_rates', schema=None) as batch_op:
        batch_op.drop_constraint('uq_pricing_rates_package_effective', type_='unique')
        batch_op.drop_index('idx_pricing_rates_package')
        batch_op.drop_column('package')
        batch_op.alter_column('income_source', existing_type=sa.String(length=20), nullable=False)
        batch_op.alter_column('taxpayer_type', existing_type=sa.String(length=20), nullable=False)
        batch_op.create_unique_constraint(
            'uq_pricing_rates_segment_effective',
            ['income_source', 'taxpayer_type', 'effective_from'],
        )
        batch_op.create_index(
            'idx_pricing_rates_lookup',
            ['income_source', 'taxpayer_type', 'effective_from'],
            unique=False,
        )
