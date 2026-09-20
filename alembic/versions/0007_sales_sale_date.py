"""add sale_date to sales

Agrega sale_date (Date, NOT NULL) a la tabla sales con índice sobre (business_id, sale_date).
Rellena filas existentes con la fecha de created_at (UTC) convertida a America/Bogota (Story 6.2).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20 20:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Agrega sale_date como nullable, rellena datos en hora Bogotá y aplica NOT NULL e índice."""
    op.add_column('sales', sa.Column('sale_date', sa.Date(), nullable=True))

    conn = op.get_bind()
    sales = conn.execute(sa.text("SELECT id, created_at FROM sales")).fetchall()
    bogota = ZoneInfo("America/Bogota")
    for row in sales:
        sale_id = row[0]
        created_at = row[1]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        sale_date = created_at.replace(tzinfo=timezone.utc).astimezone(bogota).date()
        conn.execute(
            sa.text("UPDATE sales SET sale_date = :sale_date WHERE id = :id"),
            {"sale_date": sale_date, "id": sale_id},
        )

    with op.batch_alter_table('sales') as batch_op:
        batch_op.alter_column('sale_date', existing_type=sa.Date(), nullable=False)
        batch_op.create_index('idx_sales_business_sale_date', ['business_id', 'sale_date'], unique=False)


def downgrade() -> None:
    """Quita el índice idx_sales_business_sale_date y la columna sale_date."""
    with op.batch_alter_table('sales') as batch_op:
        batch_op.drop_index('idx_sales_business_sale_date')
        batch_op.drop_column('sale_date')
