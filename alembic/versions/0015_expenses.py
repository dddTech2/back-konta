"""expenses: tabla para gastos manuales (Story 11.1)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-14 07:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'expenses',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('business_id', sa.String(length=36), nullable=False),
        sa.Column('total_amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('recorded_via', sa.String(length=20), nullable=False),
        sa.Column('recorded_by_user_id', sa.String(length=36), nullable=False),
        sa.Column('expense_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('voided_at', sa.DateTime(), nullable=True),
        sa.Column('voided_by_user_id', sa.String(length=36), nullable=True),
        sa.CheckConstraint('total_amount > 0', name='ck_expenses_total_positive'),
        sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recorded_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['voided_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_expenses_business_created', 'expenses', ['business_id', 'created_at'], unique=False)
    op.create_index('idx_expenses_business_expense_date', 'expenses', ['business_id', 'expense_date'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_expenses_business_expense_date', table_name='expenses')
    op.drop_index('idx_expenses_business_created', table_name='expenses')
    op.drop_table('expenses')
