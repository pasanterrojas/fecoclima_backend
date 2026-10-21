"""Anonymous Sentinel previews remain isolated from private parcel records."""
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.endpoints import parcel_imagery
from app.core.config import settings
from app.core.security import create_token
from app.db.models import BackgroundJob, Plot, SatelliteObservation
from app.db.session import get_db
from test_plot_integration import DATABASE, POLYGON, payload, setup as integration_setup

pytestmark = pytest.mark.skipif(not DATABASE, reason="Requires disposable FECOCLIMA_TEST_DB_URL")
PREFIX = "/public/parcel-imagery"


def polygon(offset=0.0):
    return {"type": "Polygon", "coordinates": [[
        [-75+offset, 42], [-74.999+offset, 42], [-74.999+offset, 42.001],
        [-75+offset, 42.001], [-75+offset, 42],
    ]]}


@pytest.fixture
async def imagery(integration_setup, monkeypatch):
    db, user, crop, station, private_client = integration_setup
    app = FastAPI()
    app.include_router(parcel_imagery.router)

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr("app.api.endpoints.agent_public._check_rate_limit", lambda *_args, **_kwargs: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.local",
        headers={"X-Parcel-Session": private_client.headers["X-Parcel-Session"]},
    ) as public_client:
        yield db, user, crop, station, private_client, public_client


async def test_preview_works_anonymously_before_crop_and_outside_weather_coverage(imagery):
    db, user, crop, station, private_client, client = imagery
    response = await client.post(PREFIX, json={"geojson": polygon()})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["area_ha"] > 0 and result["status"] == "PROCESSING"
    row = await db.get(Plot, UUID(result["id"]))
    assert row.crop_id is None and row.station_id is None and row.created_by is None
    assert (await client.get(f"{PREFIX}/{row.id}")).status_code == 200
    # Preview-only records do not appear in the private parcel list.
    assert (await private_client.get("/parcels")).json()["items"] == []


async def test_session_isolation_and_repeated_geometry_reuse(imagery):
    db, user, crop, station, private_client, client = imagery
    first = (await client.post(PREFIX, json={"geojson": polygon()})).json()
    repeated = (await client.post(PREFIX, json={"geojson": polygon()})).json()
    assert first["id"] == repeated["id"]
    jobs = await db.scalar(select(func.count(BackgroundJob.id)).where(
        BackgroundJob.job_type == "PLOT_SATELLITE_SYNC", BackgroundJob.payload == {"plot_id": first["id"]},
    ))
    assert jobs == 1
    client.headers["X-Parcel-Session"] = str(uuid4())
    assert (await client.get(f"{PREFIX}/{first['id']}")).status_code == 404
    other = (await client.post(PREFIX, json={"geojson": polygon()})).json()
    assert other["id"] != first["id"]
    client.headers.pop("X-Parcel-Session")
    assert (await client.post(PREFIX, json={"geojson": polygon()})).status_code == 422


async def test_previews_do_not_consume_creation_quota_and_promotion_keeps_images(imagery):
    db, user, crop, station, private_client, client = imagery
    # Ten exploratory polygons remain distinct from the three named parcels.
    first = (await client.post(PREFIX, json={"geojson": POLYGON})).json()
    for index in range(9):
        assert (await client.post(PREFIX, json={"geojson": polygon(index / 100)})).status_code == 200
    assert (await client.post(PREFIX, json={"geojson": polygon(0.2)})).status_code == 429
    observation = SatelliteObservation(
        plot_id=UUID(first["id"]), captured_at=datetime.now(UTC), provider="COPERNICUS_DATA_SPACE_SENTINEL_2_L2A",
        raw_payload={"processing_geometry": POLYGON},
    )
    db.add(observation)
    await db.commit()
    promoted = await private_client.post("/parcels", json=payload(crop.id))
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["id"] == first["id"]
    assert (await db.get(SatelliteObservation, observation.id)).plot_id == UUID(first["id"])
    assert (await client.get(f"{PREFIX}/{first['id']}")).status_code == 404
    assert (await private_client.post("/parcels", json=payload(crop.id))).json()["id"] == first["id"]
    for offset in (0.05, 0.1):
        data = {**payload(crop.id), "geojson": polygon(offset)}
        assert (await private_client.post("/parcels", json=data)).status_code == 200
    fourth = await private_client.post("/parcels", json={**payload(crop.id), "geojson": polygon(0.3)})
    assert fourth.status_code == 409


@pytest.mark.parametrize("index_name", ["ndvi", "recl"])
@pytest.mark.parametrize("visual_version", [None, "client-annex-v1"])
async def test_preview_media_tokens_are_scoped_and_revoked_when_promoted(imagery, monkeypatch, visual_version, index_name):
    db, user, crop, station, private_client, client = imagery
    result = (await client.post(PREFIX, json={"geojson": POLYGON})).json()
    plot_id = UUID(result["id"])
    root = settings.storage_path / "satellite"
    root.mkdir(parents=True, exist_ok=True)
    image, ndvi = root / "test.jpg", root / "test-ndvi.png"
    image.write_bytes(b"fixture-jpeg")
    ndvi.write_bytes(b"fixture-png")
    observation = SatelliteObservation(
        plot_id=plot_id, captured_at=datetime.now(UTC), provider="COPERNICUS_DATA_SPACE_SENTINEL_2_L2A",
        raw_payload={"processing_geometry": POLYGON, "local_image_path": str(image),
                     "local_index_image_paths": {index_name: str(ndvi)}},
    )
    db.add(observation)
    await db.commit()

    async def image_payload(*_args):
        return {"id": str(observation.id), "image_available": True,
                "index_images": {index_name: "local", "ndmi": None},
                "index_visual_versions": {index_name: visual_version} if visual_version else {}}

    monkeypatch.setattr(parcel_imagery, "satellite_observation_payload", image_payload)
    snapshot = (await client.get(f"{PREFIX}/{plot_id}")).json()
    url = snapshot["satellite"]["source_url"]
    token = parse_qs(urlsplit(url).query)["token"][0]
    good = await client.get(url)
    assert good.status_code == 200 and good.content == b"fixture-jpeg"
    assert good.headers["cache-control"] == "private, no-store"
    assert (await client.get(snapshot["satellite"]["index_images"][index_name])).content == b"fixture-png"
    query = parse_qs(urlsplit(snapshot["satellite"]["index_images"][index_name]).query)
    assert query["token"] == [token]
    assert query.get("v") == ([visual_version] if visual_version else None)
    base = f"{PREFIX}/{plot_id}/images/{observation.id}"
    assert (await client.get(f"{base}/image.jpg", params={"token": "invalid"})).status_code == 403
    expired = create_token(str(plot_id), "parcel_imagery", timedelta(seconds=-1))
    assert (await client.get(f"{base}/image.jpg", params={"token": expired})).status_code == 403
    other = create_token(str(uuid4()), "parcel_imagery", timedelta(minutes=1))
    assert (await client.get(f"{base}/image.jpg", params={"token": other})).status_code == 403
    assert (await client.get(f"{base}/ndvi", params={"token": token})).status_code == 404
    promoted = await private_client.post("/parcels", json=payload(crop.id))
    assert promoted.status_code == 200
    assert (await client.get(url)).status_code in {403, 404}


async def test_recent_legacy_render_can_be_updated_without_waiting_for_catalog_cooldown(imagery):
    db, *_, client = imagery
    first = (await client.post(PREFIX, json={"geojson": POLYGON})).json()
    job = await db.scalar(select(BackgroundJob).where(
        BackgroundJob.job_type == "PLOT_SATELLITE_SYNC", BackgroundJob.payload == {"plot_id": first["id"]},
    ))
    job.status = "COMPLETED"
    job.result = {"sentinel_2": {"visual_layers_available": 9}}
    await db.commit()
    assert (await client.post(PREFIX, json={"geojson": POLYGON})).status_code == 200
    assert await db.scalar(select(func.count(BackgroundJob.id)).where(
        BackgroundJob.job_type == "PLOT_SATELLITE_SYNC", BackgroundJob.payload == {"plot_id": first["id"]},
    )) == 2
    # The next click shares the pending update instead of spawning another job.
    assert (await client.post(PREFIX, json={"geojson": POLYGON})).status_code == 200
    assert await db.scalar(select(func.count(BackgroundJob.id)).where(
        BackgroundJob.job_type == "PLOT_SATELLITE_SYNC", BackgroundJob.payload == {"plot_id": first["id"]},
    )) == 2


async def test_preview_area_limit_prevents_unbounded_satellite_jobs(imagery):
    *_, client = imagery
    huge = {"type": "Polygon", "coordinates": [[[-75, 42], [-70, 42], [-70, 45], [-75, 45], [-75, 42]]]}
    response = await client.post(PREFIX, json={"geojson": huge})
    assert response.status_code == 422 and "25.000" in response.text

async def test_private_satellite_status_and_context_enrichment_preserve_analysis(imagery, monkeypatch):
    """Late Sentinel data must appear without a new analysis or losing other widgets."""
    from app.api.endpoints import parcels
    from app.db.models import PlotAnalysis

    db, user, crop, station, private_client, client = imagery
    first = (await client.post(PREFIX, json={'geojson': POLYGON})).json()
    promoted = await private_client.post('/parcels', json=payload(crop.id))
    assert promoted.status_code == 200, promoted.text
    plot_id = UUID(first['id'])
    assert promoted.json()['id'] == first['id']
    row = PlotAnalysis(
        plot_id=plot_id, created_by=user.id, reference_at=datetime.now(UTC),
        context={'plot': {'geojson': POLYGON}, 'satellite': None, 'radar': None,
                 'windows': {'72': {'level': 'NINGUNA'}}},
        agent_results={'ORCHESTRATOR': {'markdown': 'Antes'},
                       'SATELLITE_ANALYSIS': {'markdown': 'Sin datos'},
                       'RAG_CROP': {'markdown': 'Interpretación agro anterior sin la imagen'},
                       'WEATHER_ANALYSIS': {'markdown': 'La estación está disponible'}},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    scene = {'id': str(uuid4()), 'captured_at': (datetime.now(UTC) - timedelta(days=1)).isoformat(),
             'ndvi': 0.61, 'ndmi': 0.22, 'indices': {'ndvi': 0.61, 'ndmi': 0.22},
             'index_images': {'ndvi': f'/parcels/{plot_id}/satellite/1/ndvi.png'}}

    async def new_satellite(*_args, **_kwargs):
        return scene, None, {'latitude': -25.005, 'longitude': -55.005, 'method': 'interior_reference_only'}

    monkeypatch.setattr(parcels, 'plot_satellite_context', new_satellite)
    status = await private_client.get(f'/parcels/{plot_id}/satellite-status')
    assert status.status_code == 200, status.text
    assert status.json()['satellite']['indices']['ndvi'] == 0.61
    update = await private_client.post(f'/parcels/analyses/{row.id}/satellite/sync')
    assert update.status_code == 200, update.text
    assert update.json()['updated'] is True
    context = update.json()['analysis']['context']
    assert context['satellite']['ndvi'] == 0.61
    assert context['windows']['72']['level'] == 'NINGUNA'
    assert set(update.json()['analysis']['agent_results']) == {'WEATHER_ANALYSIS'}
    again = await private_client.post(f'/parcels/analyses/{row.id}/satellite/sync')
    assert again.json()['updated'] is False
