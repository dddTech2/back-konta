"""dian_tax_calendar por rangos de terminación

Calendario tributario que soporta todas las obligaciones (Story 4.1a): llave de NIT de 0, 1 o 2 dígitos
con rango inclusivo, cuota, periodo que cubre el vencimiento y municipio. `nit_last_digit` se conserva
(nullable) hasta que el bot migre al motor (Story 4.1b).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'dian_tax_calendar'


def upgrade() -> None:
    """Agrega las columnas de rango, rellena las filas previas e incorpora los índices."""
    # Defaults temporales: SQLite no agrega columnas NOT NULL sin ellos.
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.add_column(sa.Column('period_start', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('period_end', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('key_length', sa.SmallInteger(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('key_from', sa.SmallInteger(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('key_to', sa.SmallInteger(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('installment', sa.SmallInteger(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('jurisdiction', sa.String(length=60), nullable=False, server_default=''))
        batch_op.alter_column('nit_last_digit', existing_type=sa.Integer(), nullable=True)

    # Cada fila previa es una llave de un dígito: el rango es ese mismo dígito.
    op.execute(f"UPDATE {TABLE} SET key_from = nit_last_digit, key_to = nit_last_digit")

    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.alter_column('key_length', existing_type=sa.SmallInteger(), server_default=None)
        batch_op.alter_column('key_from', existing_type=sa.SmallInteger(), server_default=None)
        batch_op.alter_column('key_to', existing_type=sa.SmallInteger(), server_default=None)
        batch_op.alter_column('installment', existing_type=sa.SmallInteger(), server_default=None)
        batch_op.alter_column('jurisdiction', existing_type=sa.String(length=60), server_default=None)

    # La tabla anterior no tenía unicidad: si hubiera filas repetidas (p. ej. un seed ejecutado dos veces), el
    # índice único fallaría a medias y SQLite no revierte DDL. Se conserva una fila por llave.
    op.execute(
        f"DELETE FROM {TABLE} WHERE id NOT IN (SELECT MIN(id) FROM {TABLE} GROUP BY tax_type, fiscal_year,"
        " period_label, installment, jurisdiction, key_length, key_from, key_to)"
    )

    op.create_index(
        'uq_dian_tax_calendar_obligation', TABLE,
        ['tax_type', 'fiscal_year', 'period_label', 'installment', 'jurisdiction',
         'key_length', 'key_from', 'key_to'],
        unique=True,
    )
    op.create_index(
        'idx_dian_tax_calendar_lookup', TABLE,
        ['tax_type', 'fiscal_year', 'key_length', 'key_from', 'key_to'],
        unique=False,
    )


def downgrade() -> None:
    """Vuelve al esquema de un dígito; las filas que no caben en él (rangos, dos dígitos, sin llave) se pierden."""
    op.drop_index('idx_dian_tax_calendar_lookup', table_name=TABLE)
    op.drop_index('uq_dian_tax_calendar_obligation', table_name=TABLE)

    op.execute(
        f"DELETE FROM {TABLE} WHERE NOT (key_length = 1 AND key_from = key_to AND installment = 0"
        " AND jurisdiction = '')"
    )
    op.execute(f"UPDATE {TABLE} SET nit_last_digit = key_from WHERE nit_last_digit IS NULL")

    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.alter_column('nit_last_digit', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column('jurisdiction')
        batch_op.drop_column('installment')
        batch_op.drop_column('key_to')
        batch_op.drop_column('key_from')
        batch_op.drop_column('key_length')
        batch_op.drop_column('period_end')
        batch_op.drop_column('period_start')
