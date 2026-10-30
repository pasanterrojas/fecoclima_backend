"""Per-alert automatic analysis cycles and auditable simulations."""
from alembic import op
import sqlalchemy as sa
from app.db.models import NotificationEventRun
from sqlalchemy.dialects.postgresql import JSONB

revision = '0016_notification_cycles'
down_revision = '0015_unified_notifications'
branch_labels = None
depends_on = None


def upgrade():
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('notification_events')}
    for column in [sa.Column('lookback_hours', sa.Integer(), nullable=False, server_default='72'),
                   sa.Column('run_full_analysis', sa.Boolean(), nullable=False, server_default=sa.true()),
                   sa.Column('station_cursors', JSONB(), nullable=False, server_default='{}')]:
        if column.name not in columns:
            op.add_column('notification_events', column)
    # Previous UI fixed all new alerts at five minutes. Adopt the requested
    # two-hour default without changing any genuinely custom older cadence.
    op.execute('UPDATE notification_events SET every_minutes=120 WHERE every_minutes=5')
    op.execute('''UPDATE notification_events AS e SET station_cursors=(
        SELECT COALESCE(jsonb_object_agg(s.key,jsonb_build_object(
            'last_sampled_at',e.last_checked_at,'state',CASE WHEN s.value='true'::jsonb THEN 'TRUE' ELSE 'FALSE' END
        )),'{}'::jsonb) FROM jsonb_each(e.station_states) AS s
    ) WHERE e.trigger_number>0 AND e.last_checked_at IS NOT NULL AND e.station_cursors='{}'::jsonb''')
    NotificationEventRun.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    # Preserve execution history and its evidence on application rollback.
    pass
