"""create worker_heartbeats

Último contacto de cada worker con /internal/jobs/next y marca del último aviso de silencio
(Story 1.8). Sin claves foráneas.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea worker_heartbeats con el nombre del worker único."""
    op.create_table('worker_heartbeats',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=True),
    sa.Column('last_alert_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )


def downgrade() -> None:
    """Elimina worker_heartbeats."""
    op.drop_table('worker_heartbeats')
