"""revenue_split: settings table for revenue split and frozen split columns in payment_records

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16 07:00:00.000000

"""
from typing import Sequence, Union
from datetime import datetime
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Tabla revenue_split_settings
    op.create_table(
        'revenue_split_settings',
        sa.Column('package', sa.String(length=10), nullable=False),
        sa.Column('nyo_pct', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('visioncontable_pct', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by_admin_id', sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            'nyo_pct >= 0 AND visioncontable_pct >= 0 AND nyo_pct + visioncontable_pct = 100',
            name='ck_revenue_split_settings_valid',
        ),
        sa.ForeignKeyConstraint(['updated_by_admin_id'], ['users.id'], name='fk_revenue_split_admin_id_users'),
        sa.PrimaryKeyConstraint('package'),
    )

    # 2. Semilla de configuración de reparto: CON_IVA y SIN_IVA en 50.00 / 50.00
    split_table = table(
        'revenue_split_settings',
        column('package', sa.String),
        column('nyo_pct', sa.Numeric),
        column('visioncontable_pct', sa.Numeric),
        column('updated_at', sa.DateTime),
        column('updated_by_admin_id', sa.String),
    )
    now_dt = datetime(2026, 10, 16, 0, 0, 0)
    op.bulk_insert(
        split_table,
        [
            {
                'package': 'CON_IVA',
                'nyo_pct': 50.00,
                'visioncontable_pct': 50.00,
                'updated_at': now_dt,
                'updated_by_admin_id': None,
            },
            {
                'package': 'SIN_IVA',
                'nyo_pct': 50.00,
                'visioncontable_pct': 50.00,
                'updated_at': now_dt,
                'updated_by_admin_id': None,
            },
        ],
    )

    # 3. Columnas nuevas en payment_records con op.batch_alter_table
    with op.batch_alter_table('payment_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('package', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('split_nyo_pct', sa.Numeric(precision=5, scale=2), nullable=True))
        batch_op.add_column(sa.Column('nyo_amount', sa.Numeric(precision=14, scale=2), nullable=True))
        batch_op.add_column(sa.Column('visioncontable_amount', sa.Numeric(precision=14, scale=2), nullable=True))

    # 4. Relleno de payment_records con SQL compatible SQLite y PostgreSQL
    op.execute(
        """
        UPDATE payment_records
        SET
            package = COALESCE(
                (
                    SELECT s.package
                    FROM subscriptions s
                    WHERE s.id = payment_records.subscription_id
                ),
                'SIN_IVA'
            ),
            split_nyo_pct = 50.00,
            nyo_amount = ROUND(amount * 0.5),
            visioncontable_amount = amount - ROUND(amount * 0.5)
        WHERE package IS NULL
        """
    )


def downgrade() -> None:
    # 1. Quitar columnas de payment_records
    with op.batch_alter_table('payment_records', schema=None) as batch_op:
        batch_op.drop_column('visioncontable_amount')
        batch_op.drop_column('nyo_amount')
        batch_op.drop_column('split_nyo_pct')
        batch_op.drop_column('package')

    # 2. Eliminar tabla revenue_split_settings
    op.drop_table('revenue_split_settings')
