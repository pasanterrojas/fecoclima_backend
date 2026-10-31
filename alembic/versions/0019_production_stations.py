"""Install the configured real weather stations without replacing production edits.

The catalogue is an immutable release fixture, never provider credentials or readings.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

revision = "0019_production_stations"
down_revision = "0018_station_predictions"
branch_labels = None
depends_on = None
CATALOGUE_SHA256 = "c92b68d3dd8cb8051fdd5867bd4b09eb2972a1a6c65d835ea3c3d4e470046768"


def catalogue():
    path = Path(__file__).resolve().parents[2] / "app/data/stations_2026_10_31.json"
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != CATALOGUE_SHA256:
        raise RuntimeError("The frozen production station catalogue has changed")
    return json.loads(payload)["items"]


def upgrade():
    connection = op.get_bind()
    table = sa.Table("stations", sa.MetaData(), autoload_with=connection)
    # Include inactive and deleted stations: never recreate something an admin removed.
    existing = connection.execute(sa.select(table.c.code, table.c.external_guid)).all()
    codes = {code.strip().casefold() for code, guid in existing}
    guids = {guid.strip().casefold() for code, guid in existing if guid}
    for row in catalogue():
        code, guid = row["code"].strip().casefold(), row["external_guid"].strip().casefold()
        if code in codes or guid in guids:
            continue
        connection.execute(insert(table).values(
            **row, id=uuid4(), created_by=None, last_synced_at=None,
            created_at=sa.func.now(), updated_at=sa.func.now(), deleted_at=None,
        ).on_conflict_do_nothing())
        codes.add(code)
        guids.add(guid)


def downgrade():
    # Station observations, parcels and audits may already refer to these records.
    # A rollback must not erase operational data or undo deliberate CMS edits.
    pass
