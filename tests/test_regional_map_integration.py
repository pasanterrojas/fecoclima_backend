"""Signed browser ownership, job reuse, and station-only public tile delivery."""

import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.endpoints.agent_public import _rate_windows
from app.api.endpoints.public_parcels import router as parcel_router
from app.api.endpoints.regional_map import router
from app.core.config import settings
from app.db.models import BackgroundJob, Station
from app.db.session import get_db
from app.services.map_terrain import cache_dir
from app.services.regional_map import build_snapshot

DATABASE = os.environ.get("FECOCLIMA_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not DATABASE, reason="Requires disposable FECOCLIMA_TEST_DB_URL")


@pytest.mark.asyncio
async def test_analysis_ownership_dedup_and_public_raster(monkeypatch, tmp_path):
    assert DATABASE and "55439" in DATABASE
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    _rate_windows.clear()
    engine = create_async_engine(DATABASE)
    async with engine.connect() as connection:
        outer = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db:
            app = FastAPI()
            app.include_router(router)
            app.include_router(parcel_router)

            async def session():
                yield db

            app.dependency_overrides[get_db] = session
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                identity = (
                    await client.post(
                        "/public/parcels/browser-session", headers={"X-Parcel-Session": str(uuid4())}
                    )
                ).json()
                headers = {
                    "X-Parcel-Access": identity["access_token"],
                    "X-Parcel-Session": identity["browser_id"],
                }
                # Cookies alone do not authorize a mutation.
                assert (await client.post("/public/map/analyses")).status_code == 401
                first = await client.post("/public/map/analyses", headers=headers)
                assert first.status_code == 200
                job_id = first.json()["job_id"]
                again = await client.post("/public/map/analyses", headers=headers)
                assert again.json()["job_id"] == job_id
                assert (await client.get(f"/public/map/analyses/{job_id}", headers=headers)).json()[
                    "status"
                ] == "PENDING"
                client.cookies.clear()
                other = (
                    await client.post(
                        "/public/parcels/browser-session", headers={"X-Parcel-Session": str(uuid4())}
                    )
                ).json()
                other_headers = {
                    "X-Parcel-Access": other["access_token"],
                    "X-Parcel-Session": other["browser_id"],
                }
                assert (
                    await client.get(f"/public/map/analyses/{job_id}", headers=other_headers)
                ).status_code == 404
                job = await db.get(BackgroundJob, UUID(job_id))
                job.status = "COMPLETED"
                folder = cache_dir() / "snapshots"
                folder.mkdir()
                (folder / f"{job_id}.json").write_text(
                    json.dumps({"stations": [], "variables": [{"id": "rain", "minimum": 0, "maximum": 100}]})
                )
                await db.commit()
                assert (await client.get(f"/public/map/analyses/{job_id}", headers=headers)).json()[
                    "snapshot"
                ]["stations"] == []
                client.cookies.clear()
                response = await client.get(f"/public/map/analyses/{job_id}/tiles/rain/7/43/73.png")
                assert response.status_code == 200 and response.headers["content-type"] == "image/png"
                assert "public" in response.headers["cache-control"]
                assert (
                    await client.get(f"/public/map/analyses/{job_id}/tiles/private/7/43/73.png")
                ).status_code == 404
            await db.rollback()
        await outer.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_covers_active_stations_with_and_without_sensors(monkeypatch, tmp_path):
    assert DATABASE and "55439" in DATABASE
    engine = create_async_engine(DATABASE)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db:
            active = Station(
                name="Sin humedad",
                code=uuid4().hex,
                active=True,
                source_type="MANUAL",
                latitude=-25,
                longitude=-55,
                influence_radius_m=1000,
            )
            inactive = Station(name="No incluir", code=uuid4().hex, active=False, source_type="MANUAL")
            db.add_all([active, inactive])
            await db.flush()
            snapshot = await build_snapshot(db, datetime.now(UTC))
            result = next(s for s in snapshot["stations"] if s["id"] == str(active.id))
            assert result["values"]["humidity"] is None
            assert result["influence_geojson"]["type"] == "Polygon"
            assert not any(s["id"] == str(inactive.id) for s in snapshot["stations"])
            assert "producer" not in json.dumps(snapshot)
        await transaction.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_refreshes_all_active_stations_and_keeps_snapshot_on_one_failure(monkeypatch, tmp_path):
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import session as db_module
    from app.services import source_refresh
    from app.services.regional_map import run_regional_analysis

    assert DATABASE and "55439" in DATABASE
    engine = create_async_engine(DATABASE)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    monkeypatch.setattr(db_module, "AsyncSessionLocal", factory)
    ids = []
    called = []
    running = 0
    peak = 0
    async with factory() as db:
        entries = [
            Station(
                name=f"Mapa prueba {i}",
                code=uuid4().hex,
                active=i < 4,
                source_type="MANUAL",
                latitude=-25,
                longitude=-55,
                influence_radius_m=1000,
            )
            for i in range(5)
        ]
        db.add_all(entries)
        await db.commit()
        ids = [s.id for s in entries]

    async def refresh(session, entry, **kwargs):
        nonlocal running, peak
        assert kwargs == {"requested": True}
        called.append(entry.id)
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.01)
            if entry.id == ids[0]:
                raise RuntimeError("Private provider error")
            return {"status": "COMPLETED"}
        finally:
            running -= 1

    monkeypatch.setattr(source_refresh, "refresh_station_source_if_needed", refresh)
    try:
        async with factory() as db:
            job_id = uuid4()
            result = await run_regional_analysis(db, job_id)
            saved = json.loads((cache_dir() / "snapshots" / f"{job_id}.json").read_text())
        assert set(ids[:4]).issubset(called) and ids[4] not in called
        assert peak <= 3
        assert result["station_count"] >= 4
        assert (
            next(s for s in saved["source_refresh"] if s["station_id"] == str(ids[0]))["status"]
            == "UNAVAILABLE"
        )
        assert "Private provider error" not in json.dumps(saved)
        assert saved["window_hours"] == 72
    finally:
        async with factory() as db:
            await db.execute(delete(Station).where(Station.id.in_(ids)))
            await db.commit()
        await engine.dispose()
