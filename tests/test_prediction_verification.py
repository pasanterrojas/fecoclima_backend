"""Disposable PostgreSQL only: pre-day freezing, audit evidence, nullable outcomes."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.models import AuditLog, Station, StationForecastVerification
from app.services.station_prediction import predict_days, save_predictions, verify_observations

DATABASE = os.environ.get("FECOCLIMA_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not DATABASE, reason="Requires disposable FECOCLIMA_TEST_DB_URL")


@pytest.mark.asyncio
async def test_latest_prediction_frozen_before_target_then_observations_filled():
    assert DATABASE and "55439" in DATABASE
    engine = create_async_engine(DATABASE)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db:
            station = Station(
                code=uuid4().hex[:12],
                name="Predicción de prueba",
                timezone="UTC",
                latitude=-25,
                longitude=-55,
            )
            db.add(station)
            await db.flush()
            start = datetime(2026, 10, 30, 6, tzinfo=UTC)
            models = [
                {
                    "date": (start.date() + timedelta(days=i)).isoformat(),
                    "temperature_avg_c": 25,
                    "precipitation_mm": 3,
                }
                for i in range(1, 4)
            ]
            first = predict_days([], models, start, "UTC")
            await save_predictions(db, station, first, start)
            await db.flush()
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(StationForecastVerification)
                    .where(StationForecastVerification.station_id == station.id)
                )
                == 3
            )
            later = start + timedelta(hours=10)
            second = predict_days([], [dict(r, precipitation_mm=9) for r in models], later, "UTC")
            await save_predictions(db, station, second, later)
            await db.flush()
            row = await db.scalar(
                select(StationForecastVerification).where(
                    StationForecastVerification.station_id == station.id,
                    StationForecastVerification.target_date == start.date() + timedelta(days=1),
                )
            )
            await db.refresh(row)
            assert row.predicted["precipitation_mm"] == 9 and row.issued_at == later
            # A delayed job and a backdated retry must not replace the issued forecast.
            await save_predictions(db, station, first, start)
            await save_predictions(db, station, first, start + timedelta(days=1))
            await db.refresh(row)
            assert row.predicted["precipitation_mm"] == 9 and row.issued_at == later
            history = [
                {
                    "date": row.target_date.isoformat(),
                    "values": {"precipitation_mm": 5},
                    "coverage": {"precipitation_mm": 1},
                }
            ]
            await verify_observations(db, station, history, start + timedelta(days=2))
            await db.flush()
            await db.refresh(row)
            assert row.observed["precipitation_mm"] == 5
            assert row.observed["temperature_avg_c"] is None
            assert row.predicted["precipitation_mm"] == 9
            audit = list((await db.scalars(select(AuditLog).where(AuditLog.entity_id == str(row.id)))).all())
            assert {r.action for r in audit} == {
                "STATION_PREDICTION_GENERATED",
                "STATION_PREDICTION_VERIFIED",
            }
            await db.rollback()
        await transaction.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_late_model_response_is_not_backdated_into_a_closed_prediction_day(monkeypatch):
    from app.services import station_prediction as service

    assert DATABASE and "55439" in DATABASE
    reference = datetime(2026, 10, 30, 23, 59, 50, tzinfo=UTC)
    completed = reference + timedelta(seconds=20)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return completed

    monkeypatch.setattr(service, "datetime", Clock)
    engine = create_async_engine(DATABASE)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint") as db:
            station = Station(code=uuid4().hex[:12], name="Respuesta tardía", timezone="UTC",
                              latitude=-25, longitude=-55, integration_config={})
            db.add(station)
            await db.flush()

            async def model(*args, **kwargs):
                return [{"date": (reference.date()+timedelta(days=i)).isoformat(),
                         "temperature_avg_c":25} for i in range(1,4)]

            await service.station_prediction(db, station, reference, fetcher=model)
            rows = list((await db.scalars(select(StationForecastVerification).where(
                StationForecastVerification.station_id==station.id))).all())
            assert len(rows)==2  # Tomorrow already began before the calculation finished.
            assert all(row.issued_at==completed for row in rows)
            assert all(row.target_date>completed.date() for row in rows)
            await db.rollback()
        await transaction.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_prediction_migration_preserves_a_table_created_by_initial_metadata():
    """A fresh installation creates current metadata before revision 0018 runs."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    assert DATABASE and "55439" in DATABASE
    path = Path(__file__).parents[1] / "alembic/versions/0018_station_prediction_verification.py"
    spec = importlib.util.spec_from_file_location("prediction_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_async_engine(DATABASE)
    try:
        async with engine.begin() as connection:
            before = await connection.scalar(text("SELECT count(*) FROM station_forecast_verifications"))

            def upgrade(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.upgrade()

            await connection.run_sync(upgrade)
            after = await connection.scalar(text("SELECT count(*) FROM station_forecast_verifications"))
            assert after == before
    finally:
        await engine.dispose()
