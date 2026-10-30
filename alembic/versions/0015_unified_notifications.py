"""Avisos y alertas: preferencias consentidas, códigos y entregas recuperables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0015_unified_notifications'
down_revision = '0014_public_parcel_browser'
branch_labels = None
depends_on = None


def upgrade():
    def add(table, column):
        if column.name not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, column)
    for table in ('email_subscriptions', 'notification_events'):
        for column in ('crop_id', 'station_id'):
            op.alter_column(table, column, nullable=True)
    add('email_subscriptions', sa.Column('email_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    add('email_subscriptions', sa.Column('telegram_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    add('email_subscriptions', sa.Column('telegram_username', sa.String(40)))
    add('email_subscriptions', sa.Column('consent_at', sa.DateTime(timezone=True)))
    add('email_subscriptions', sa.Column('consent_version', sa.String(40)))
    op.execute("UPDATE email_subscriptions SET telegram_enabled = telegram_active, consent_at = confirmed_at, consent_version = 'legacy-opt-in' WHERE consent_version IS NULL")
    # PostgreSQL NULLs are distinct: explicitly protect general book entries too.
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_subscription_general_email ON email_subscriptions (email) WHERE crop_id IS NULL AND station_id IS NULL')
    add('notification_events', sa.Column('code', sa.String(40)))
    op.execute("UPDATE notification_events SET code = 'ALT-' || upper(replace(id::text, '-', '')) WHERE code IS NULL")
    op.alter_column('notification_events', 'code', nullable=False)
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_event_code ON notification_events (code)')
    add('notification_events', sa.Column('station_states', JSONB(), nullable=False, server_default='{}'))
    add('notification_event_deliveries', sa.Column('payload', JSONB(), nullable=False, server_default='{}'))
    add('notification_event_deliveries', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    add('notification_event_deliveries', sa.Column('next_attempt_at', sa.DateTime(timezone=True)))
    # No resume of legacy sending jobs after switching to the single monitor.
    op.execute("UPDATE background_jobs SET status = 'COMPLETED', result = '{\"status\":\"RETIRED\"}'::jsonb WHERE job_type IN ('DAILY_PRODUCER_DIGEST', 'PLOT_ALERT_MONITOR', 'PLOT_ALERT_DISPATCH') AND status = 'PENDING'")


def downgrade():
    # Keep subscriber consent and delivery evidence intact on application rollback.
    pass
