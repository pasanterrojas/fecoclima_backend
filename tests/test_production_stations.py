"""Frozen station deployment, idempotency, and preservation of existing CMS records."""
import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from shapely.geometry import shape
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Station

DATABASE = os.environ.get("FECOCLIMA_TEST_DB_URL")


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/0019_production_stations.py"
    spec = importlib.util.spec_from_file_location("production_stations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalogue_contains_real_stations_and_only_nonsecret_configuration():
    items = migration().catalogue()
    assert len(items) == 11
    assert len({row["external_guid"].casefold() for row in items}) == 11
    for row in items:
        assert row["source_type"] == "FECOCLIMA_API"
        assert row["active"] is True
        assert "DEMO" not in row["code"]
        assert "id" not in row and "created_by" not in row and "last_synced_at" not in row
        assert row["import_config"] == {}
        assert set(row["integration_config"]) <= {
            "map_color", "water_aggregation", "soil_moisture_unit", "soil_tension_proxy_enabled",
            "soil_tension_rule_validated", "header_name", "coordinate_note", "range_max_months",
            "coordinate_source",
        }
        assert row["integration_config"]["water_aggregation"]["rain_mm"] == "daily_counter"
        assert -90 <= row["latitude"] <= 90 and -180 <= row["longitude"] <= 180
        if row["influence_geojson"]:
            assert shape(row["influence_geojson"]).is_valid


@pytest.mark.skipif(not DATABASE, reason="Requires disposable FECOCLIMA_TEST_DB_URL")
@pytest.mark.asyncio
async def test_migration_imports_once_preserves_edits_and_respects_removed_guid():
    assert DATABASE and "55439" in DATABASE
    module = migration()
    items = module.catalogue()
    engine = create_async_engine(DATABASE)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            # Only the disposable database, rolled back at the end.
            await connection.execute(delete(Station))
            first, removed = items[:2]
            existing_id, removed_id = uuid4(), uuid4()
            await connection.execute(Station.__table__.insert().values(
                **{**first, "name": "Nombre editado en producción", "latitude": -24.1234,
                   "integration_config": {"map_color": "#123456"}}, id=existing_id,
            ))
            await connection.execute(Station.__table__.insert().values(
                **{**removed, "code": "CODIGO-CAMBIADO", "external_guid": removed["external_guid"].lower(),
                   "active": False}, id=removed_id, deleted_at=datetime.now(UTC),
            ))

            def upgrade(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    module.upgrade()

            await connection.run_sync(upgrade)
            await connection.run_sync(upgrade)
            rows = (await connection.execute(select(Station.__table__))).mappings().all()
            assert len(rows) == 11
            existing = next(row for row in rows if row["id"] == existing_id)
            assert existing["name"] == "Nombre editado en producción"
            assert existing["latitude"] == -24.1234
            assert existing["integration_config"] == {"map_color": "#123456"}
            deleted = next(row for row in rows if row["id"] == removed_id)
            assert not deleted["active"] and deleted["deleted_at"]
            for expected in items[2:]:
                imported = next(row for row in rows if row["external_guid"] == expected["external_guid"])
                for field, value in expected.items():
                    assert imported[field] == value, field
            await transaction.rollback()
    finally:
        await engine.dispose()
