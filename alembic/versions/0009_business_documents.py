"""create business_documents

Crea la tabla business_documents para almacenar documentos de clientes
cargados por la administración y consultados desde el panel web (Story 7.4a).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-06 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea la tabla business_documents con índices y claves foráneas."""
    op.create_table(
        'business_documents',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('business_id', sa.String(length=36), nullable=False),
        sa.Column('doc_type', sa.String(length=50), nullable=False),
        sa.Column('description', sa.String(length=120), nullable=True),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('storage_key', sa.String(length=500), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('uploaded_by_user_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by_user_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ['business_id'],
            ['businesses.id'],
            name='fk_business_documents_business_id_businesses',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['uploaded_by_user_id'],
            ['users.id'],
            name='fk_business_documents_uploaded_by_user_id_users',
        ),
        sa.ForeignKeyConstraint(
            ['deleted_by_user_id'],
            ['users.id'],
            name='fk_business_documents_deleted_by_user_id_users',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_business_documents_business_id'),
        'business_documents',
        ['business_id'],
        unique=False,
    )


def downgrade() -> None:
    """Elimina el índice y la tabla business_documents."""
    op.drop_index(op.f('ix_business_documents_business_id'), table_name='business_documents')
    op.drop_table('business_documents')
