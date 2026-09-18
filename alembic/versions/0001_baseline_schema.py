"""baseline schema

Esquema base de las 9 tablas de models.py (adopción de Alembic, Story 1.5).

Revision ID: 0001
Revises: 
Create Date: 2026-09-18 18:20:54.097914

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea las 9 tablas del esquema base."""
    op.create_table('dian_tax_calendar',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('tax_type', sa.String(length=50), nullable=False),
    sa.Column('fiscal_year', sa.Integer(), nullable=False),
    sa.Column('period_label', sa.String(length=100), nullable=False),
    sa.Column('nit_last_digit', sa.Integer(), nullable=False),
    sa.Column('deadline_date', sa.Date(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_dian_tax_calendar_fiscal_year'), 'dian_tax_calendar', ['fiscal_year'], unique=False)
    op.create_index(op.f('ix_dian_tax_calendar_nit_last_digit'), 'dian_tax_calendar', ['nit_last_digit'], unique=False)
    op.create_index(op.f('ix_dian_tax_calendar_tax_type'), 'dian_tax_calendar', ['tax_type'], unique=False)

    op.create_table('users',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=255), nullable=False),
    sa.Column('phone', sa.String(length=30), nullable=True),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('telegram_chat_id', sa.BigInteger(), nullable=True),
    sa.Column('telegram_username', sa.String(length=100), nullable=True),
    sa.Column('is_telegram_linked', sa.Boolean(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_role'), 'users', ['role'], unique=False)
    op.create_index(op.f('ix_users_telegram_chat_id'), 'users', ['telegram_chat_id'], unique=True)

    op.create_table('businesses',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('client_id', sa.String(length=36), nullable=False),
    sa.Column('legal_name', sa.String(length=255), nullable=False),
    sa.Column('commercial_name', sa.String(length=255), nullable=False),
    sa.Column('nit', sa.String(length=30), nullable=False),
    sa.Column('dv', sa.String(length=2), nullable=False),
    sa.Column('taxpayer_type', sa.String(length=20), nullable=False),
    sa.Column('legal_rep_doc', sa.String(length=30), nullable=True),
    sa.Column('economic_activity', sa.String(length=255), nullable=True),
    sa.Column('invoice_prefix_filter', sa.String(length=50), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_businesses_client_id'), 'businesses', ['client_id'], unique=False)
    op.create_index(op.f('ix_businesses_nit'), 'businesses', ['nit'], unique=False)

    op.create_table('subscriptions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('client_id', sa.String(length=36), nullable=False),
    sa.Column('plan', sa.String(length=20), nullable=False),
    sa.Column('discount_rate', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('base_price', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('final_price', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('cutoff_date', sa.Date(), nullable=False),
    sa.Column('grace_period_end', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_notified_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_subscriptions_client_id'), 'subscriptions', ['client_id'], unique=False)
    op.create_index(op.f('ix_subscriptions_cutoff_date'), 'subscriptions', ['cutoff_date'], unique=False)
    op.create_index(op.f('ix_subscriptions_status'), 'subscriptions', ['status'], unique=False)

    op.create_table('telegram_link_tokens',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('token', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('is_used', sa.Boolean(), nullable=False),
    sa.Column('used_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_telegram_link_tokens_token'), 'telegram_link_tokens', ['token'], unique=True)
    op.create_index(op.f('ix_telegram_link_tokens_user_id'), 'telegram_link_tokens', ['user_id'], unique=False)

    op.create_table('dian_extraction_jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('business_id', sa.String(length=36), nullable=False),
    sa.Column('target_period', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('next_run_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('zip_path', sa.String(length=500), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('screenshot_path', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_dian_extraction_jobs_business_id'), 'dian_extraction_jobs', ['business_id'], unique=False)
    op.create_index(op.f('ix_dian_extraction_jobs_next_run_at'), 'dian_extraction_jobs', ['next_run_at'], unique=False)
    op.create_index(op.f('ix_dian_extraction_jobs_status'), 'dian_extraction_jobs', ['status'], unique=False)

    op.create_table('invoices',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('business_id', sa.String(length=36), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('document_type', sa.String(length=100), nullable=False),
    sa.Column('cufe', sa.String(length=255), nullable=False),
    sa.Column('folio', sa.String(length=50), nullable=True),
    sa.Column('prefix', sa.String(length=50), nullable=True),
    sa.Column('currency', sa.String(length=10), nullable=False),
    sa.Column('payment_form', sa.String(length=50), nullable=True),
    sa.Column('payment_method', sa.String(length=50), nullable=True),
    sa.Column('issue_date', sa.DateTime(), nullable=False),
    sa.Column('reception_date', sa.DateTime(), nullable=True),
    sa.Column('issuer_nit', sa.String(length=30), nullable=False),
    sa.Column('issuer_name', sa.String(length=255), nullable=False),
    sa.Column('receiver_nit', sa.String(length=30), nullable=False),
    sa.Column('receiver_name', sa.String(length=255), nullable=False),
    sa.Column('iva', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('ica', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('inc', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('timbre', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('inc_bolsas', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('in_carbono', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('in_combustibles', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('ibua', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('icui', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('rete_iva', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('rete_renta', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('rete_ica', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('dian_status', sa.String(length=100), nullable=True),
    sa.Column('group_type', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_invoices_business_id'), 'invoices', ['business_id'], unique=False)
    op.create_index(op.f('ix_invoices_cufe'), 'invoices', ['cufe'], unique=True)
    op.create_index(op.f('ix_invoices_group_type'), 'invoices', ['group_type'], unique=False)
    op.create_index(op.f('ix_invoices_issue_date'), 'invoices', ['issue_date'], unique=False)

    op.create_table('monthly_tax_summaries',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('business_id', sa.String(length=36), nullable=False),
    sa.Column('period_year_month', sa.String(length=7), nullable=False),
    sa.Column('total_invoiced_net', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('iva_generado', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('iva_descontable', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('iva_balance', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('rete_iva_total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('rete_renta_total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('rete_ica_total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('total_invoices_count', sa.Integer(), nullable=False),
    sa.Column('variation_vs_previous_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('calculated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('business_id', 'period_year_month', name='uq_business_period')
    )
    op.create_index(op.f('ix_monthly_tax_summaries_business_id'), 'monthly_tax_summaries', ['business_id'], unique=False)
    op.create_index(op.f('ix_monthly_tax_summaries_period_year_month'), 'monthly_tax_summaries', ['period_year_month'], unique=False)

    op.create_table('payment_records',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('subscription_id', sa.String(length=36), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('payment_date', sa.Date(), nullable=False),
    sa.Column('payment_method', sa.String(length=50), nullable=False),
    sa.Column('reference_code', sa.String(length=100), nullable=True),
    sa.Column('verified_by_admin_id', sa.String(length=36), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['verified_by_admin_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payment_records_subscription_id'), 'payment_records', ['subscription_id'], unique=False)



def downgrade() -> None:
    """Elimina las 9 tablas respetando el orden de las FK."""
    op.drop_index(op.f('ix_payment_records_subscription_id'), table_name='payment_records')
    op.drop_table('payment_records')
    op.drop_index(op.f('ix_monthly_tax_summaries_period_year_month'), table_name='monthly_tax_summaries')
    op.drop_index(op.f('ix_monthly_tax_summaries_business_id'), table_name='monthly_tax_summaries')
    op.drop_table('monthly_tax_summaries')
    op.drop_index(op.f('ix_invoices_issue_date'), table_name='invoices')
    op.drop_index(op.f('ix_invoices_group_type'), table_name='invoices')
    op.drop_index(op.f('ix_invoices_cufe'), table_name='invoices')
    op.drop_index(op.f('ix_invoices_business_id'), table_name='invoices')
    op.drop_table('invoices')
    op.drop_index(op.f('ix_dian_extraction_jobs_status'), table_name='dian_extraction_jobs')
    op.drop_index(op.f('ix_dian_extraction_jobs_next_run_at'), table_name='dian_extraction_jobs')
    op.drop_index(op.f('ix_dian_extraction_jobs_business_id'), table_name='dian_extraction_jobs')
    op.drop_table('dian_extraction_jobs')
    op.drop_index(op.f('ix_telegram_link_tokens_user_id'), table_name='telegram_link_tokens')
    op.drop_index(op.f('ix_telegram_link_tokens_token'), table_name='telegram_link_tokens')
    op.drop_table('telegram_link_tokens')
    op.drop_index(op.f('ix_subscriptions_status'), table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_cutoff_date'), table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_client_id'), table_name='subscriptions')
    op.drop_table('subscriptions')
    op.drop_index(op.f('ix_businesses_nit'), table_name='businesses')
    op.drop_index(op.f('ix_businesses_client_id'), table_name='businesses')
    op.drop_table('businesses')
    op.drop_index(op.f('ix_users_telegram_chat_id'), table_name='users')
    op.drop_index(op.f('ix_users_role'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_dian_tax_calendar_tax_type'), table_name='dian_tax_calendar')
    op.drop_index(op.f('ix_dian_tax_calendar_nit_last_digit'), table_name='dian_tax_calendar')
    op.drop_index(op.f('ix_dian_tax_calendar_fiscal_year'), table_name='dian_tax_calendar')
    op.drop_table('dian_tax_calendar')
