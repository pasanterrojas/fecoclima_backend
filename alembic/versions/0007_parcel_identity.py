"""Parcel identity, browser quotas and the six-condition traffic light."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0007_parcel_identity'
down_revision = '0006_plot_intelligence'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('plots')}
    for column in [sa.Column('producer_document', sa.String(30), nullable=True),
                   sa.Column('browser_session_id', postgresql.UUID(as_uuid=True), nullable=True),
                   sa.Column('geometry_key', sa.String(64), nullable=True)]:
        if column.name not in columns: op.add_column('plots', column)
    indexes = {index['name'] for index in inspector.get_indexes('plots')}
    for field in ['browser_session_id', 'geometry_key']:
        if f'ix_plots_{field}' not in indexes: op.create_index(f'ix_plots_{field}', 'plots', [field])
    # Existing published versions keep their other rules/thresholds. Change only
    # the count-to-colour mapping requested by the product owner.
    bind = op.get_bind()
    import json
    from app.services.alert_engine import DEFAULT_ALERT_CONFIGURATION
    for row in bind.execute(sa.text('SELECT id, configuration FROM crop_rule_versions')).mappings():
        config = row['configuration'] or {}
        if isinstance(config, str): config = json.loads(config)
        target = config.get('alert') if isinstance(config.get('alert'), dict) else config
        target['levels'] = DEFAULT_ALERT_CONFIGURATION['levels']
        target.pop('alert_thresholds', None)
        for key in ('moderate_min', 'severe_count', 'severe_min', 'moderate_max'): target.pop(key, None)
        bind.execute(sa.text('UPDATE crop_rule_versions SET configuration=CAST(:config AS jsonb) WHERE id=:id'), {'config':json.dumps(config), 'id':row['id']})
    # Retire generated, redundant agents reversibly; retain versions, documents,
    # audit logs and any agent customized by an administrator.
    bind.execute(sa.text("""UPDATE ai_agents SET active=false, deleted_at=now()
      WHERE created_by IS NULL AND deleted_at IS NULL
      AND (purpose IN ('CROP_ANALYSIS','YEAR_COMPARISON') OR code LIKE 'AGENT_MAIZ_%')
      AND code LIKE 'AGENT_%'"""))

    from app.services.agent_defaults import default_agent_prompt
    legacy_suffix = 'Tu función específica es entregar una recomendación práctica usando toda la consulta disponible. La primera frase debe decir al productor qué atención requiere el lote y cuál es la primera acción.'
    rows = bind.execute(sa.text("""SELECT v.id, v.system_prompt, a.purpose, c.name AS crop_name
      FROM ai_agent_versions v JOIN ai_agents a ON a.id=v.agent_id JOIN crops c ON c.id=a.crop_id
      WHERE a.deleted_at IS NULL AND a.created_by IS NULL AND a.code LIKE 'AGENT_%'
      AND v.version=a.current_version""")).mappings()
    for row in rows:
        if (row['system_prompt'] or '').endswith(legacy_suffix):
            bind.execute(sa.text('UPDATE ai_agent_versions SET system_prompt=:prompt, updated_at=now() WHERE id=:id'),
                         {'prompt':default_agent_prompt(row['crop_name'],row['purpose']), 'id':row['id']})


def downgrade():
    op.drop_index('ix_plots_geometry_key', table_name='plots')
    op.drop_index('ix_plots_browser_session_id', table_name='plots')
    op.drop_column('plots','geometry_key')
    op.drop_column('plots','browser_session_id')
    op.drop_column('plots','producer_document')
