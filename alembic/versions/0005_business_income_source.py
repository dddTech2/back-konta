"""business income_source y perfil tributario

Tipo de negocio (DIAN o ventas manuales) y perfil tributario (periodicidad de IVA y agente de retención)
para el calendario y las cifras por tipo de cliente (Story 6.1). Los negocios existentes quedan `DIAN`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Agrega las tres columnas; el server_default rellena los negocios existentes (SQLite lo exige en NOT NULL)."""
    op.add_column('businesses', sa.Column('income_source', sa.String(length=20), nullable=False,
                                          server_default='DIAN'))
    op.add_column('businesses', sa.Column('iva_periodicity', sa.String(length=20), nullable=True))
    op.add_column('businesses', sa.Column('is_withholding_agent', sa.Boolean(), nullable=False,
                                          server_default=sa.false()))


def downgrade() -> None:
    """Quita las columnas del tipo de negocio y del perfil tributario."""
    with op.batch_alter_table('businesses') as batch_op:
        batch_op.drop_column('is_withholding_agent')
        batch_op.drop_column('iva_periodicity')
        batch_op.drop_column('income_source')
