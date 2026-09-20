"""add voided_at and voided_by_user_id to sales

Agrega voided_at (DateTime, nullable) y voided_by_user_id (String(36), nullable, FK a users.id)
a la tabla sales para permitir la anulación conservando el registro de auditoría (Story 6.5).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-20 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Agrega columnas voided_at y voided_by_user_id con clave foránea a users.id."""
    with op.batch_alter_table('sales') as batch_op:
        batch_op.add_column(sa.Column('voided_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('voided_by_user_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_sales_voided_by_user_id_users',
            'users',
            ['voided_by_user_id'],
            ['id'],
        )


def downgrade() -> None:
    """Quita la clave foránea fk_sales_voided_by_user_id_users y las columnas voided_by_user_id y voided_at."""
    with op.batch_alter_table('sales') as batch_op:
        batch_op.drop_constraint('fk_sales_voided_by_user_id_users', type_='foreignkey')
        batch_op.drop_column('voided_by_user_id')
        batch_op.drop_column('voided_at')
