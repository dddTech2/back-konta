"""create tax_deadline_alerts

Crea la tabla tax_deadline_alerts para registrar las alertas proactivas
de vencimientos tributarios enviadas u omitidas por Telegram (Story 4.1c).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea la tabla tax_deadline_alerts con índices y restricción única."""
    op.create_table(
        'tax_deadline_alerts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('business_id', sa.String(length=36), nullable=False),
        sa.Column('tax_type', sa.String(length=50), nullable=False),
        sa.Column('period_label', sa.String(length=100), nullable=False),
        sa.Column('installment', sa.Integer(), nullable=False),
        sa.Column('deadline_date', sa.Date(), nullable=False),
        sa.Column('moment', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('skip_reason', sa.String(length=30), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['business_id'],
            ['businesses.id'],
            name='fk_tax_deadline_alerts_business_id_businesses',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'business_id',
            'tax_type',
            'period_label',
            'installment',
            'deadline_date',
            'moment',
            name='uq_tax_deadline_alerts_obligation_moment',
        ),
    )
    op.create_index(
        op.f('ix_tax_deadline_alerts_business_id'),
        'tax_deadline_alerts',
        ['business_id'],
        unique=False,
    )


def downgrade() -> None:
    """Elimina el índice y la tabla tax_deadline_alerts."""
    op.drop_index(op.f('ix_tax_deadline_alerts_business_id'), table_name='tax_deadline_alerts')
    op.drop_table('tax_deadline_alerts')
