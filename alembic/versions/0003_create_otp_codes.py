"""create otp_codes

Códigos OTP del login web por Telegram (Story 5.1): solo se guarda el hash del código.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-19 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea otp_codes con el índice usado por el rate-limit por usuario."""
    op.create_table('otp_codes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('code_hash', sa.String(length=255), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('is_used', sa.Boolean(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_otp_user_created', 'otp_codes', ['user_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Elimina otp_codes y su índice."""
    op.drop_index('idx_otp_user_created', table_name='otp_codes')
    op.drop_table('otp_codes')
