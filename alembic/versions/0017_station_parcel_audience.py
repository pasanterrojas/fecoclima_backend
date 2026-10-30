"""Route station alerts only to consented contacts of geometrically covered parcels."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from app.db.models import SubscriptionPlot

revision='0017_station_parcel_audience'
down_revision='0016_notification_cycles'
branch_labels=None
depends_on=None


def upgrade():
    if 'pending_changes' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('email_subscriptions')}:
        op.add_column('email_subscriptions',sa.Column('pending_changes',JSONB(),nullable=False,server_default='{}'))
    SubscriptionPlot.__table__.create(op.get_bind(),checkfirst=True)
    # Never guess ownership from names/emails or turn old broadcasts into parcel subscriptions.
    op.execute("UPDATE notification_events SET active=false,last_status='NEEDS_STATION',last_detail='Seleccione una estación y vincule los contactos con sus parcelas.' WHERE station_id IS NULL")
    op.execute("UPDATE notification_event_deliveries SET status='CANCELLED',error='Entrega anterior sin vínculo verificable a una parcela; revise la audiencia.' WHERE status IN ('QUEUED','ERROR') AND NOT (payload ? 'plot_ids')")


def downgrade():
    pass
