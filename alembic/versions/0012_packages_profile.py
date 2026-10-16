"""packages_profile: add vat_responsible to businesses and backfill

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import column, table


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Agregar columna vat_responsible a businesses
    with op.batch_alter_table('businesses', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('vat_responsible', sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    # 2. Relleno (Data backfill): true donde income_source = 'DIAN', false en el resto
    businesses_tbl = table(
        'businesses',
        column('income_source', sa.String),
        column('vat_responsible', sa.Boolean),
    )
    op.execute(
        businesses_tbl.update()
        .where(businesses_tbl.c.income_source == 'DIAN')
        .values(vat_responsible=True)
    )
    op.execute(
        businesses_tbl.update()
        .where(businesses_tbl.c.income_source != 'DIAN')
        .values(vat_responsible=False)
    )


def downgrade() -> None:
    # 1. Convertir income_source = 'MIXED' en 'MANUAL_SALES'
    businesses_tbl = table(
        'businesses',
        column('income_source', sa.String),
    )
    op.execute(
        businesses_tbl.update()
        .where(businesses_tbl.c.income_source == 'MIXED')
        .values(income_source='MANUAL_SALES')
    )

    # 2. Quitar la columna vat_responsible
    with op.batch_alter_table('businesses', schema=None) as batch_op:
        batch_op.drop_column('vat_responsible')
