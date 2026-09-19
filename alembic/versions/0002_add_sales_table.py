"""add sales table

Ventas declaradas por el cliente (Story 2.4): total, descripción opcional y canal de origen.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19 18:45:32.219111

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea la tabla sales con CHECK (total_amount > 0) e índice por negocio y fecha."""
    op.create_table('sales',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('business_id', sa.String(length=36), nullable=False),
    sa.Column('total_amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('recorded_via', sa.String(length=20), nullable=False),
    sa.Column('recorded_by_user_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('total_amount > 0', name='ck_sales_total_positive'),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recorded_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_sales_business_created', 'sales', ['business_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Elimina la tabla sales."""
    op.drop_index('idx_sales_business_created', table_name='sales')
    op.drop_table('sales')
