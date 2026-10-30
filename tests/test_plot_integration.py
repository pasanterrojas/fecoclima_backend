"""Integration checks require an explicitly supplied disposable PostgreSQL."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.deps import get_current_user
from app.api.endpoints.parcels import router
from app.core.config import settings
from app.db.models import (
    BaseCondition,
    Crop,
    CropRuleSet,
    CropRuleVersion,
    PhenologyStage,
    Plot,
    PlotAlertDelivery,
    PlotAnalysis,
    Role,
    Station,
    User,
    WeatherReading,
)
from app.db.session import get_db
from app.services.plot_alerts import dispatch_plot_alerts, monitor_plot_rules
from app.services.plot_reporting import generate_plot_report

DATABASE = os.environ.get("FECOCLIMA_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not DATABASE, reason="Requires disposable FECOCLIMA_TEST_DB_URL")
POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-55.01, -25.01], [-55, -25.01], [-55, -25], [-55.01, -25], [-55.01, -25.01]]],
}


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    assert DATABASE and "55439" in DATABASE, "Use the isolated test container, never the application database"
    from app.api.endpoints.agent_public import _rate_windows
    _rate_windows.clear()
    engine = create_async_engine(DATABASE)
    monkeypatch.setattr(settings, "ai_api_key", None)
    monkeypatch.setattr(settings, "edge_ai_base_url", None)
    monkeypatch.setattr(settings, "storage_path", tmp_path)

    async def forecast(*args, **kwargs):
        today = datetime.now(UTC).date()
        return [
            {
                "date": (today + timedelta(days=i)).isoformat(),
                "temperature_min_c": 18,
                "temperature_max_c": 30,
                "humidity_pct": 70,
                "precipitation_mm": 2,
            }
            for i in range(4)
        ]

    monkeypatch.setattr("app.services.plot_pipeline.fetch_open_meteo", forecast)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db:
            role = Role(code="productor_" + uuid4().hex[:8], name="Productor", permissions={})
            db.add(role)
            await db.flush()
            user = User(
                email=f"{uuid4().hex}@test.local",
                name="Productor",
                role_id=role.id,
                password_hash="not-a-real-password",
                active=True,
            )
            db.add(user)
            crop = Crop(code="TRIGO", name="Trigo", active=True)
            db.add(crop)
            await db.flush()
            station = Station(
                name="Estación central",
                code=uuid4().hex,
                latitude=-25.005,
                longitude=-55.005,
                influence_radius_m=2000,
                active=True,
                source_type="MANUAL",
                expected_interval_minutes=60,
                timezone="America/Asuncion",
                leaf_sensor_config={"mode": "hours"},
            )
            db.add(station)
            rule_set = CropRuleSet(crop_id=crop.id, name="Reglas trigo", active=True)
            db.add(rule_set)
            await db.flush()
            rule = CropRuleVersion(
                rule_set_id=rule_set.id,
                crop_id=crop.id,
                name="Regla validada",
                version="1",
                status="PUBLISHED",
                configuration={},
            )
            db.add(rule)
            await db.flush()
            for i, variable in enumerate(
                [
                    "temperature_avg_c",
                    "humidity_avg_pct",
                    "rain_sum_mm",
                    "soil_temperature_avg_c",
                    "soil_moisture_avg_pct",
                    "leaf_wetness_hours",
                ]
            ):
                db.add(
                    BaseCondition(
                        rule_version_id=rule.id,
                        key=f"c{i}",
                        label=variable,
                        variable=variable,
                        operator="gt",
                        threshold_value=1,
                        unit="%" if "moisture" in variable else None,
                        required=True,
                        order_index=i,
                        active=True,
                    )
                )
            db.add(
                PhenologyStage(
                    crop_id=crop.id,
                    code="V1",
                    name="Vegetativo",
                    order_index=1,
                    duration_days=121,
                    start_day=0,
                    end_day=120,
                    active=True,
                )
            )
            now = datetime.now(UTC)
            for i in range(120):
                db.add(
                    WeatherReading(
                        station_id=station.id,
                        sampled_at=now - timedelta(hours=i),
                        temperature_c=26,
                        humidity_pct=90,
                        rain_mm=1,
                        soil_temperature_c=23,
                        soil_moisture_pct=65,
                        leaf_sensor_value=12,
                    )
                )
            await db.commit()
            app = FastAPI()
            app.include_router(router)

            async def database_override():
                yield db

            async def user_override():
                return user

            app.dependency_overrides[get_db] = database_override
            app.dependency_overrides[get_current_user] = user_override
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test.local",
                cookies={settings.csrf_cookie_name: "csrf-test"},
                headers={"X-CSRF-Token": "csrf-test", "X-Parcel-Session": str(uuid4()), "X-Chat-Browser": str(uuid4())},
            ) as client:
                yield db, user, crop, station, client
        await transaction.rollback()
    await engine.dispose()


def payload(crop_id):
    return {
        "producer_name": "Productor de prueba", "producer_document": "1234567", "name": "Parcela de prueba",
        "crop_id": str(crop_id),
        "planting_date": (date.today() - timedelta(days=30)).isoformat(),
        "geojson": POLYGON,
    }


async def test_full_parcel_api_workflow_and_ownership(setup):
    db, user, crop, station, client = setup
    created = await client.post("/parcels", json=payload(crop.id))
    assert created.status_code == 200, created.text
    plot_id = created.json()["id"]
    assert created.json()["area_ha"] > 0
    influences = (await client.get(f"/parcels/{plot_id}/influences")).json()
    assert influences["coverage_pct"] == 100
    result = await client.post(f"/parcels/{plot_id}/analyses")
    assert result.status_code == 200, result.text
    analysis = result.json()
    assert set(analysis["context"]["windows"]) == {"72"}
    assert analysis["context"]["selected_window_hours"] == 72
    assert analysis["context"]["windows"]["72"]["level"] == "GRAVE"
    assert len(analysis["context"]["windows"]["72"]["stations"][0]["alert"]["conditions"]) == 6
    assert len(analysis["context"]["forecast"]["days"]) == 3
    interpreted = await client.post(
        f"/parcels/analyses/{analysis['id']}/interpret", json={"purpose": "ORCHESTRATOR"}
    )
    assert interpreted.status_code == 200, interpreted.text
    assert interpreted.json()["status"] == "fallback"
    chat = await client.post(f"/parcels/analyses/{analysis['id']}/chat", data={"message": "¿Qué reviso hoy?"})
    assert chat.status_code == 200, chat.text
    history = (await client.get(f"/parcels/analyses/{analysis['id']}/chat")).json()
    assert len(history["messages"]) == 2
    report = await client.post(f"/parcels/analyses/{analysis['id']}/reports")
    assert report.status_code == 200, report.text

    pdf = await generate_plot_report(db, report.json()["id"])
    assert pdf["size_bytes"] > 500
    downloaded = await client.get(f"/parcels/reports/{report.json()['id']}/file")
    assert downloaded.status_code == 200 and downloaded.content.startswith(b"%PDF-")
    foreign = Plot(
        name="Parcela ajena",
        crop_id=crop.id,
        station_id=station.id,
        geojson=POLYGON,
        planting_date=date.today(),
        created_by=None,
        active=True,
    )
    db.add(foreign)
    await db.commit()
    assert (await client.get(f"/parcels/{foreign.id}/influences")).status_code == 404
    listing = (await client.get("/parcels")).json()["items"]
    assert len(listing) == 1 and listing[0]["id"] == plot_id
    assert (
        await client.post("/parcels", json=payload(crop.id), headers={"X-CSRF-Token": "bad"})
    ).status_code == 403


@pytest.mark.parametrize('hours', [60, 72, 96, 120])
async def test_dual_soil_criterion_uses_station_unit_and_new_analysis_is_clean(setup, hours, monkeypatch):
    from unittest.mock import Mock
    from app.services import plot_pipeline

    evaluate = Mock(wraps=plot_pipeline.evaluate_window)
    monkeypatch.setattr(plot_pipeline, 'evaluate_window', evaluate)
    db, user, crop, station, client = setup
    soil = await db.scalar(select(BaseCondition).where(BaseCondition.variable == 'soil_moisture_avg_pct'))
    soil.operator = 'gte'
    soil.threshold_value = 50
    soil.soil_alternative = {'unit':'cb','operator':'lte','threshold_value':35,
                             'source_reference':'Umbral sintético de prueba; no recomendación agronómica'}
    station.integration_config = {'soil_moisture_unit':'cb'}
    await db.commit()
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    first = (await client.post(f"/parcels/{created['id']}/analyses", json={'window_hours':hours})).json()
    assert set(first['context']['windows']) == {str(hours)}
    assert first['context']['selected_window_hours'] == hours
    assert len(first['context']['forecast']['days']) == 3
    assert [day['date'] for day in first['context']['forecast']['days']] == [
        (datetime.fromisoformat(first['reference_at']).astimezone(ZoneInfo(station.timezone)).date()
         + timedelta(days=offset)).isoformat() for offset in (1, 2, 3)
    ]
    assert evaluate.call_count == 1  # One station, only the requested rule evaluation.
    assert evaluate.call_args.args[0]['hours'] == hours
    window = first['context']['windows'][str(hours)]
    assert window['stations'][0]['expected_samples'] == hours
    condition = next(item for item in window['stations'][0]['alert']['conditions'] if item['key'] == soil.key)
    assert condition['unit'] == 'cb' and condition['value'] == 65
    assert condition['status'] == 'not_met'
    assert window['level'] != 'DATOS_INSUFICIENTES'
    assert len(window['stations'][0]['alert']['conditions']) == 6
    wrong_hours = 120 if hours != 120 else 60
    wrong = await client.post(f"/parcels/analyses/{first['id']}/interpret", json={'purpose':'ORCHESTRATOR','window_hours':wrong_hours})
    assert wrong.status_code == 422
    wrong_chat = await client.post(f"/parcels/analyses/{first['id']}/chat", data={'message':'¿Qué reviso?', 'window_hours':wrong_hours})
    assert wrong_chat.status_code == 422
    interpreted = await client.post(f"/parcels/analyses/{first['id']}/interpret", json={'purpose':'ORCHESTRATOR','window_hours':hours})
    assert interpreted.status_code == 200 and interpreted.json()['window_hours'] == hours
    station.integration_config = {'soil_moisture_unit':'%'}
    await db.commit()
    second = (await client.post(f"/parcels/{created['id']}/analyses")).json()
    assert second['id'] != first['id'] and second['agent_results'] == {}
    condition = next(item for item in second['context']['windows']['72']['stations'][0]['alert']['conditions'] if item['key'] == soil.key)
    assert (condition['unit'], condition['value'], condition['status']) == ('%',65,'met')


async def test_unsupported_analysis_window_does_not_create_snapshot(setup):
    db, user, crop, station, client = setup
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    response = await client.post(f"/parcels/{created['id']}/analyses", json={'window_hours':48})
    assert response.status_code == 422
    assert await db.scalar(select(PlotAnalysis).where(PlotAnalysis.plot_id == created['id'])) is None


async def test_orchestrator_distinguishes_observed_hours_from_three_future_days(setup, monkeypatch):
    from app.db.models import AIAgent, AIAgentVersion
    from app.services import plot_agents

    db, user, crop, station, client = setup
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    analysis = (await client.post(f"/parcels/{created['id']}/analyses", json={'window_hours':120})).json()
    agent = AIAgent(name='Orquestador', code=uuid4().hex, purpose='ORCHESTRATOR', crop_id=crop.id,
                    active=True, current_version='1.0.0')
    db.add(agent)
    await db.flush()
    db.add(AIAgentVersion(agent_id=agent.id, version='1.0.0', active=True, model='fixture',
                          provider='EDGE', system_prompt='Explique la parcela'))
    await db.commit()
    captured = {}

    async def generated(prompt, context, *args, **kwargs):
        captured.update(prompt=prompt, context=context)
        return {'markdown': 'Revise el cultivo según los datos observados. Considere el pronóstico de los próximos tres días.', 'usage': {}}

    monkeypatch.setattr(plot_agents, 'generate_markdown_response', generated)
    response = await client.post(f"/parcels/analyses/{analysis['id']}/interpret",
                                 json={'purpose':'ORCHESTRATOR', 'window_hours':120})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'completed'
    assert captured['context']['temporal_scope']['observations']['hours'] == 120
    assert captured['context']['temporal_scope']['forecast']['horizon_days'] == 3
    assert len(captured['context']['temporal_scope']['forecast']['dates']) == 3
    assert 'historical_regression' in captured['context']
    assert 'PRÓXIMOS TRES DÍAS' in captured['prompt']
    assert 'no está limitada a las horas seleccionadas' in captured['prompt']


async def test_parcel_image_history_is_private_and_matches_current_polygon(setup):
    from app.db.models import SatelliteObservation
    db, user, crop, station, client = setup
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    now = datetime.now(UTC)
    for days, value, geometry in [(1,0.65,POLYGON), (5,0.4,POLYGON), (2,0.9,{'type':'Polygon','coordinates':[]})]:
        db.add(SatelliteObservation(plot_id=created['id'], captured_at=now-timedelta(days=days),
            provider='COPERNICUS_DATA_SPACE_SENTINEL_2_L2A', ndvi=value,
            raw_payload={'processing_geometry':geometry,'indices':{'ndvi':value}}))
    await db.commit()
    result = await client.get(f"/parcels/{created['id']}/satellite/history")
    assert result.status_code == 200, result.text
    assert [scene['ndvi'] for scene in result.json()['items']] == [0.65,0.4]
    assert result.json()['scope'] == 'processed_scenes_current_geometry'
    foreign = Plot(name='Otra parcela',geojson=POLYGON,crop_id=crop.id,created_by=None,active=True)
    db.add(foreign); await db.commit()
    assert (await client.get(f'/parcels/{foreign.id}/satellite/history')).status_code == 404


async def test_admin_details_preserve_permissions_and_hide_credentials(setup):
    """Detail pages share serializers, reject missing IDs and protect admin data."""
    from app.api.endpoints import admin, integrations, resources, stations
    from app.db.models import AIAgent, AIAgentVersion, AuditLog, IntegrationConfig

    db, user, crop, station, _ = setup
    app = FastAPI()
    for module in (resources, stations, integrations, admin):
        app.include_router(module.router)

    async def database_override():
        yield db

    async def user_override():
        return user

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_current_user] = user_override
    agent = AIAgent(name="Agro", code=uuid4().hex, crop_id=crop.id, purpose="RAG_CROP")
    integration = IntegrationConfig(name=uuid4().hex, provider="COPERNICUS_DATA_SPACE", integration_type="SATELLITE", encrypted_secret=b"private-test-credential")
    audit = AuditLog(action="TEST", actor_email=user.email, before_data={"name": "Antes"}, after_data={"name": "Después"})
    db.add_all([agent, integration, audit])
    await db.flush()
    db.add(AIAgentVersion(agent_id=agent.id, version="1.0.0", system_prompt="Interprete los datos", model="test-model"))
    await db.flush()
    plot = Plot(name="Parcela", crop_id=crop.id, station_id=station.id, geojson=POLYGON, planting_date=date.today(), active=True)
    db.add(plot)
    await db.flush()
    paths = [f"/crops/{crop.id}", f"/stations/{station.id}", f"/plots/{plot.id}", f"/agents/{agent.id}", f"/integrations/{integration.id}", f"/audit/{audit.id}"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.local") as client:
        # The fixture starts with a producer; detail URLs must not bypass roles.
        for path in paths:
            assert (await client.get(path)).status_code == 403
        role = Role(code="superadmin", name="Administrador", permissions={})
        db.add(role)
        await db.flush()
        user.role_id = role.id
        await db.flush()
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 200, response.text
            assert response.json()["id"] == path.split("/")[-1]
        agent_data = (await client.get(paths[3])).json()
        satellite = await client.get(f"/plots/{plot.id}/satellite")
        assert satellite.status_code == 200 and satellite.json() is None
        assert agent_data["category"] == "AGRO" and agent_data["crop_scope"] == str(crop.id)
        assert agent_data["system_prompt"] == "Interprete los datos"
        integration_data = (await client.get(paths[4])).json()
        assert "encrypted_secret" not in integration_data
        assert "private-test-credential" not in str(integration_data)
        assert (await client.get(paths[5])).json()["after_data"] == {"name": "Después"}
        other_crop = Crop(code=uuid4().hex[:20], name="Otro cultivo", active=True)
        db.add(other_crop)
        await db.flush()
        assert (await client.get("/rules", params={"crop_id": str(other_crop.id)})).json()["items"] == []
        assert len((await client.get("/rules", params={"crop_id": str(crop.id)})).json()["items"]) == 1
        for base in ("crops", "stations", "plots", "agents", "integrations", "audit"):
            assert (await client.get(f"/{base}/{uuid4()}")).status_code == 404
        agent.deleted_at = datetime.now(UTC)
        await db.flush()
        assert (await client.get(paths[3])).status_code == 404


async def test_crop_knowledge_inheritance_and_thematic_constellation(setup):
    from app.db.models import AIAgent, AgentKnowledgeDocument, KnowledgeDocument, KnowledgeChunk
    from app.services.agent_constellation import compact_generated_agents
    from app.services.agent_defaults import build_default_agent
    from app.services.agent_runtime import select_agent_version
    from app.services.crop_knowledge import sync_crop_knowledge
    from app.services.rag import retrieve_chunks

    db, user, crop, station, client = setup
    soy = Crop(code="SOJA",name="Soja",active=True)
    db.add(soy); await db.flush()
    purposes=("RAG_CROP","ORCHESTRATOR","WEATHER_ANALYSIS","SATELLITE_ANALYSIS","CROP_CHAT","DAILY_DIGEST","PDF_REPORT","ALERT_ANALYSIS","PHENOLOGY_ANALYSIS","DISEASE_ANALYSIS","FORECAST_ANALYSIS")
    for item in (crop,soy):
        for purpose in purposes:
            agent,version=build_default_agent(item,purpose=purpose)
            version.model="fixture-model"
            db.add_all([agent,version])
    await db.flush()
    result=await compact_generated_agents(db)
    assert result["shared_created"]==4 and len(result["archived"])==18
    active=list((await db.scalars(select(AIAgent).where(AIAgent.active.is_(True),AIAgent.deleted_at.is_(None)))).all())
    assert len(active)==8
    assert (await compact_generated_agents(db))["shared_created"]==0
    agro,_=await select_agent_version(db,crop.id,"DISEASE_ANALYSIS")
    assert agro.purpose=="RAG_CROP" and agro.crop_id==crop.id
    data,_=await select_agent_version(db,crop.id,"FORECAST_ANALYSIS")
    assert data.purpose=="WEATHER_ANALYSIS" and data.crop_id is None
    first=KnowledgeDocument(title="Manual trigo",crop_id=crop.id,index_status="INDEXED",active=True)
    other=KnowledgeDocument(title="Solo soja",crop_id=soy.id,index_status="INDEXED",active=True)
    db.add_all([first,other]);await db.flush()
    db.add_all([KnowledgeChunk(document_id=first.id,crop_id=crop.id,chunk_index=0,content="humedad trigo",page_or_section="1"),KnowledgeChunk(document_id=other.id,crop_id=soy.id,chunk_index=0,content="humedad soja",page_or_section="1")]);await db.flush()
    assert await sync_crop_knowledge(db,crop.id)==1
    assert await sync_crop_knowledge(db,crop.id)==1
    links=list((await db.scalars(select(AgentKnowledgeDocument).where(AgentKnowledgeDocument.agent_id==agro.id))).all())
    assert len(links)==1
    chunks=await retrieve_chunks(db,"humedad",crop.id,agent_id=agro.id,allow_embedding=False)
    assert {item["document"] for item in chunks}=={"Manual trigo"}
    # A shared specialist inherits the request's crop without a permanent link.
    chunks=await retrieve_chunks(db,"humedad",crop.id,agent_id=data.id,allow_embedding=False)
    assert {item["document"] for item in chunks}=={"Manual trigo"}
    assert await sync_crop_knowledge(db,soy.id,agro.id)==1  # does not reassign the agent
    first.active=False;await db.flush()
    assert await retrieve_chunks(db,"humedad",crop.id,agent_id=agro.id,allow_embedding=False)==[]


async def test_crop_uploads_sync_existing_and_new_agents_without_duplicates(setup):
    from app.api.endpoints import knowledge, resources
    from app.db.models import AgentKnowledgeDocument
    db,user,crop,station,_=setup
    role=Role(code="superadmin",name="Administrador",permissions={});db.add(role);await db.flush()
    user.role_id=role.id;await db.flush()
    app=FastAPI();app.include_router(knowledge.router);app.include_router(resources.router)
    async def database_override(): yield db
    async def user_override(): return user
    app.dependency_overrides[get_db]=database_override;app.dependency_overrides[get_current_user]=user_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test.local",cookies={settings.csrf_cookie_name:"test"},headers={"X-CSRF-Token":"test"}) as client:
        first=await client.post("/knowledge/upload",data={"title":"Manual","crop_id":str(crop.id)},files={"file":("manual.txt",b"Manual agronomico de prueba","text/plain")})
        assert first.status_code==200,first.text
        payload={"name":"Experto","code":uuid4().hex,"crop_id":str(crop.id),"purpose":"RAG_CROP","provider":"EDGE","model":"fixture-model"}
        created=await client.post("/agents",json=payload)
        assert created.status_code==200,created.text
        assert created.json()["documents_count"]==1
        second=await client.post("/knowledge/upload",data={"title":"Anexo","crop_id":str(crop.id)},files={"file":("anexo.txt",b"Anexo del cultivo de prueba","text/plain")})
        assert second.status_code==200,second.text
        repeated=await client.post("/knowledge/upload",data={"title":"Anexo","crop_id":str(crop.id)},files={"file":("anexo.txt",b"Anexo del cultivo de prueba","text/plain")})
        assert repeated.status_code==200 and repeated.json()["id"]==second.json()["id"]
        detail=(await client.get(f"/agents/{created.json()['id']}")).json()
        assert detail["documents_count"]==2
        assert len((await client.get("/knowledge/documents",params={"crop_id":str(crop.id)})).json()["items"])==2
        assert (await client.post(f"/knowledge/crops/{crop.id}/sync")).json()["documents_count"]==2
        links=list((await db.scalars(select(AgentKnowledgeDocument).where(AgentKnowledgeDocument.agent_id==created.json()["id"]))).all())
        assert len(links)==2


async def test_no_coverage_and_geometry_update_preserve_snapshots(setup):
    db, user, crop, station, client = setup
    created = (await client.post("/parcels", json=payload(crop.id))).json()
    first = (await client.post(f"/parcels/{created['id']}/analyses")).json()
    updated = payload(crop.id)
    updated["geojson"] = {
        "type": "Polygon",
        "coordinates": [[[-58.01, -25.01], [-58, -25.01], [-58, -25], [-58.01, -25], [-58.01, -25.01]]],
    }
    assert (await client.put(f"/parcels/{created['id']}", json=updated)).status_code == 200
    latest = (await client.post(f"/parcels/{created['id']}/analyses")).json()
    assert latest["context"]["station_influences"] == []
    assert latest["context"]["windows"]["72"]["level"] == "DATOS_INSUFICIENTES"
    assert latest["context"]["plot"]["station_id"] is None
    old = await db.get(PlotAnalysis, first["id"])
    assert old.context["plot"]["geojson"] == POLYGON


async def test_alert_cooldown_outbox_and_channel_dispatch(setup, monkeypatch):
    db, user, crop, station, client = setup
    created = (await client.post("/parcels", json=payload(crop.id))).json()
    rule = await client.post(
        f"/parcels/{created['id']}/alert-rules",
        json={
            "natural_language": "Avísame si la humedad supera 80% y temp supera 25°C",
            "channel": "EMAIL",
            "recipient": "producer@example.org",
            "cooldown_minutes": 180,
        },
    )
    assert rule.status_code == 200, rule.text
    sent = []

    async def fake_send(recipients, subject, message):
        sent.append(recipients)

    monkeypatch.setattr("app.services.plot_alerts._send_email", fake_send)
    assert (await monitor_plot_rules(db))["queued"] == 1
    assert (await monitor_plot_rules(db))["queued"] == 0
    assert (await dispatch_plot_alerts(db))["sent"] == 1
    assert sent == [["producer@example.org"]]
    assert (await dispatch_plot_alerts(db))["sent"] == 0
    delivery = await db.scalar(select(PlotAlertDelivery))
    assert delivery.status == "SENT"
    assert (
        await client.delete(f"/parcels/{created['id']}/alert-rules/{rule.json()['id']}")
    ).status_code == 200
    assert (await monitor_plot_rules(db))["queued"] == 0


async def test_seed_creates_versions_for_every_configurable_agent(setup):
    from app.db.models import AIAgent, AIAgentVersion
    from app.services.agent_defaults import AGENT_PURPOSES
    from app.services.seed import _seed_agent

    db, user, crop, station, client = setup
    for purpose in AGENT_PURPOSES:
        await _seed_agent(db, crop, purpose)
    await db.flush()
    agents = list((await db.scalars(select(AIAgent).where(AIAgent.crop_id == crop.id))).all())
    assert {agent.purpose for agent in agents} == set(AGENT_PURPOSES)
    versions = list((await db.scalars(select(AIAgentVersion))).all())
    assert len(versions) == len(AGENT_PURPOSES)
    assert all(version.provider == "OPENAI" for version in versions)


async def test_invalid_crop_does_not_stop_background_rule_monitor(setup):
    db, user, crop, station, client = setup
    created = (await client.post("/parcels", json=payload(crop.id))).json()
    rule = await client.post(
        f"/parcels/{created['id']}/alert-rules",
        json={
            "natural_language": "humedad supera 80",
            "channel": "EMAIL",
            "recipient": "producer@example.org",
        },
    )
    assert rule.status_code == 200
    crop.active = False
    await db.commit()
    assert (await monitor_plot_rules(db))["queued"] == 0


async def test_reanalysis_reuses_polygon_and_three_per_browser_session(setup):
    db, user, crop, station, client = setup
    original = payload(crop.id)
    first = await client.post('/parcels', json=original)
    assert first.status_code == 200, first.text
    original_id = first.json()['id']
    again = await client.post('/parcels', json=original)
    assert again.json()['id'] == original_id
    # Ring rotation/orientation must not create another parcel.
    import copy
    rotated = copy.deepcopy(original)
    ring = rotated['geojson']['coordinates'][0][:-1]
    ring = list(reversed(ring[1:] + ring[:1]))
    rotated['geojson']['coordinates'][0] = ring + [ring[0]]
    assert (await client.post('/parcels', json=rotated)).json()['id'] == original_id
    for offset in (0.001, 0.002):
        other = copy.deepcopy(original)
        other['geojson']['coordinates'][0] = [[x+offset,y] for x,y in original['geojson']['coordinates'][0]]
        response = await client.post('/parcels', json=other)
        assert response.status_code == 200, response.text
    fourth = copy.deepcopy(original)
    fourth['geojson']['coordinates'][0] = [[x+0.003,y] for x,y in original['geojson']['coordinates'][0]]
    rejected = await client.post('/parcels', json=fourth)
    assert rejected.status_code == 409
    assert '3 parcelas' in rejected.json()['detail']
    assert (await client.post('/parcels', json=original)).json()['id'] == original_id
    items = (await client.get('/parcels')).json()['items']
    assert len(items) == 3
    assert all(item['producer_document'] == '1234567' for item in items)
    # A new browser session has its own quota; existing parcels still reuse IDs.
    assert (await client.post('/parcels', json=fourth, headers={'X-Parcel-Session':str(uuid4())})).status_code == 200


async def test_general_orchestrator_uses_published_current_version(setup):
    from app.db.models import AIAgent, AIAgentVersion
    from app.services.agent_runtime import select_agent_version
    db, user, crop, station, client = setup
    agent = AIAgent(name='Orquestador general',code=uuid4().hex,purpose='ORCHESTRATOR',crop_id=None,current_version='2.0.0',active=True)
    db.add(agent)
    await db.flush()
    published = AIAgentVersion(agent_id=agent.id,version='2.0.0',system_prompt='Orquestador vigente',active=True,provider='EDGE')
    newer = AIAgentVersion(agent_id=agent.id,version='3.0.0',system_prompt='Borrador no publicado',active=True,provider='OPENAI')
    db.add_all([published,newer])
    await db.flush()
    selected, version = await select_agent_version(db,crop.id,'ORCHESTRATOR')
    assert selected.id == agent.id
    assert version.id == published.id


async def test_browser_quota_is_shared_when_switching_users(setup):
    db, user, crop, station, client = setup
    import copy
    base = payload(crop.id)
    for offset in (0, 0.001, 0.002):
        item = copy.deepcopy(base)
        item['geojson']['coordinates'][0] = [[x+offset,y] for x,y in base['geojson']['coordinates'][0]]
        assert (await client.post('/parcels',json=item)).status_code == 200
    other = User(email=f'{uuid4().hex}@test.local',name='Otro productor',role_id=user.role_id,password_hash='test-only',active=True)
    db.add(other)
    await db.flush()
    async def second_user(): return other
    client._transport.app.dependency_overrides[get_current_user] = second_user
    fourth = copy.deepcopy(base)
    fourth['geojson']['coordinates'][0] = [[x+0.003,y] for x,y in base['geojson']['coordinates'][0]]
    response = await client.post('/parcels',json=fourth)
    assert response.status_code == 409


async def test_admin_can_reactivate_current_agent_version(setup):
    from app.db.models import AIAgent, AIAgentVersion
    from app.api.endpoints.resources import update_agent, _agent_dict
    from app.schemas.resources import AgentPayload
    from app.services.agent_runtime import select_agent_version
    from starlette.requests import Request
    db, user, crop, station, client = setup
    agent = AIAgent(name='Agente desactivado',code=uuid4().hex,purpose='RAG_CROP',crop_id=crop.id,current_version='1.0.0',active=False)
    db.add(agent)
    await db.flush()
    version = AIAgentVersion(agent_id=agent.id,version='1.0.0',system_prompt='Criterio configurado',model='edge-test',provider='EDGE',active=False)
    db.add(version)
    await db.flush()
    assert (await _agent_dict(db,agent))['model'] == 'edge-test'
    request = Request({'type':'http','method':'PUT','path':'/agents','headers':[], 'client':('127.0.0.1',1)})
    payload = AgentPayload(name=agent.name,code=agent.code,crop_id=crop.id,purpose='RAG_CROP',active=True,system_prompt='Criterio configurado',model='edge-test',provider='EDGE')
    await update_agent(agent.id,payload,request,db,user)
    selected, published = await select_agent_version(db,crop.id,'RAG_CROP')
    assert selected.id == agent.id
    assert published.id == version.id
    assert published.active


async def test_chat_histories_and_images_are_isolated_between_browsers(setup, monkeypatch):
    """Same account and analysis do not imply a shared browser conversation."""
    from app.db.models import PlotConversation

    db, user, crop, station, client = setup
    browser_one, browser_two = uuid4(), uuid4()
    created = (await client.post('/parcels', json=payload(crop.id))).json()
    analysis = PlotAnalysis(plot_id=created['id'], reference_at=datetime.now(UTC),
                            context={'crop': {'id': str(crop.id)}, 'selected_window_hours':72,
                                     'windows':{'72':{'hours':72}}}, agent_results={}, created_by=user.id)
    db.add(analysis)
    await db.flush()
    # Legacy rows are retained but never assigned to an arbitrary new browser.
    db.add(PlotConversation(analysis_id=analysis.id, user_id=user.id, browser_session_id=None,
                            messages=[{'role': 'user', 'content': 'Historial de otro navegador anterior'}]))
    await db.commit()
    received = []

    async def fake_interpret(_db, _analysis, _purpose, **kwargs):
        received.append({'history': list(kwargs['history']), 'images': list(kwargs['images'])})
        return {'markdown': 'Respuesta según los datos observados.', 'status': 'completed', 'sources': []}

    monkeypatch.setattr('app.api.endpoints.parcels.interpret_plot', fake_interpret)
    monkeypatch.setattr('app.api.endpoints.agent_public._check_rate_limit', lambda *_args, **_kwargs: None)
    url = f'/parcels/analyses/{analysis.id}/chat'
    client.headers['X-Chat-Browser'] = str(browser_one)
    assert (await client.get(url)).json() == {'messages': []}
    photo = b'\x89PNG\r\n\x1a\nfixture-private-image'
    first = await client.post(url, data={'message': 'Revise esta foto'}, files={'images': ('campo.png', photo, 'image/png')})
    assert first.status_code == 200, first.text
    assert received[-1]['history'] == [] and len(received[-1]['images']) == 1
    assert 'storage_path' not in first.json()['messages'][0]['images'][0]
    assert len((await client.get(url)).json()['messages']) == 2
    client.headers['X-Chat-Browser'] = str(browser_two)
    assert (await client.get(url)).json() == {'messages': []}
    second = await client.post(url, data={'message': 'Mi consulta independiente'})
    assert second.status_code == 200, second.text
    assert received[-1] == {'history': [], 'images': []}
    assert len((await client.get(url)).json()['messages']) == 2
    client.headers['X-Chat-Browser'] = str(browser_one)
    assert (await client.get(url)).json()['messages'][0]['content'] == 'Revise esta foto'
    repeated = await client.post(url, data={'message': '¿Qué cambió?'})
    assert repeated.status_code == 200, repeated.text
    assert len(received[-1]['history']) == 2 and len(received[-1]['images']) == 1
    assert len((await client.get(url)).json()['messages']) == 4
    rows = list((await db.scalars(select(PlotConversation).where(PlotConversation.analysis_id == analysis.id))).all())
    assert len(rows) == 3
    client.headers.pop('X-Chat-Browser')
    assert (await client.get(url)).status_code == 422
    assert (await client.post(url, data={'message': 'Sin identificador'})).status_code == 422


async def test_soil_unit_safety_is_consistent_in_daily_alerts_and_rule_simulation(setup):
    """Synthetic thresholds exercise units, not an agronomic recommendation."""
    from zoneinfo import ZoneInfo

    from app.services.rule_validation import simulate_rule
    from app.services.weather import aggregate_station_day, evaluate_alert_for_day

    db, user, crop, source, client = setup
    source.integration_config = {"soil_moisture_unit": "cb"}
    await db.commit()
    day = datetime.now(UTC).astimezone(ZoneInfo(source.timezone)).date() - timedelta(days=1)
    planting = day - timedelta(days=30)
    await aggregate_station_day(db, source, day)
    rule = await db.scalar(select(CropRuleVersion).where(CropRuleVersion.crop_id == crop.id))
    daily = await evaluate_alert_for_day(db, source.id, crop.id, day, planting)
    simulated = await simulate_rule(db, rule.id, source.id, day, day, planting)
    for result in (daily, simulated["days"][0]):
        soil = next(item for item in result["conditions"] if item["key"] == "c4")
        assert result["level"] == "DATOS_INSUFICIENTES"
        assert soil["status"] == "missing" and soil["missing_reason"] == "incompatible_unit"
        assert soil["observed_unit"] == "cb"
        assert result["values_used"]["soil_moisture_avg_pct"] is None
        assert result["values_used"]["soil_tension_avg_cb"] == 65

    condition = await db.scalar(select(BaseCondition).where(
        BaseCondition.rule_version_id == rule.id, BaseCondition.key == "c4",
    ))
    condition.variable, condition.unit = "soil_tension_avg_cb", "cb"
    condition.operator, condition.threshold_value = "lte", 100
    condition.label = "Criterio sintético de tensión para prueba"
    await db.commit()
    daily = await evaluate_alert_for_day(db, source.id, crop.id, day, planting)
    simulated = await simulate_rule(db, rule.id, source.id, day, day, planting)
    for result in (daily, simulated["days"][0]):
        soil = next(item for item in result["conditions"] if item["key"] == "c4")
        assert result["level"] == "GRAVE" and result["conditions_met"] == 6
        assert (soil["status"], soil["value"], soil["unit"]) == ("met", 65, "cb")


async def test_new_active_crop_uses_its_configured_normal_phenology_calendar(setup):
    db, user, crop, station, client = setup
    crop.code, crop.name = "MAIZ_PRUEBA", "Maíz de prueba"
    for code, cycle, deleted in (("ALT", "experimental", None), ("RETIRADA", "normal", datetime.now(UTC))):
        db.add(PhenologyStage(
            crop_id=crop.id, code=code, name="No se debe mezclar", cycle_type=cycle,
            order_index=2, duration_days=121, start_day=0, end_day=120,
            active=True, deleted_at=deleted,
        ))
    await db.commit()
    response = await client.post("/parcels", json=payload(crop.id))
    assert response.status_code == 200, response.text
    analyzed = await client.post(f"/parcels/{response.json()['id']}/analyses")
    assert analyzed.status_code == 200, analyzed.text
    context = analyzed.json()["context"]
    assert context["crop"]["code"] == "MAIZ_PRUEBA"
    assert context["phenology"]["stage_code"] == "V1"
    assert len(context["phenology"]["timeline"]) == 1


async def test_parcel_color_persists_without_duplicate_parcel_or_quota_use(setup):
    db, user, crop, station, client = setup
    body = {**payload(crop.id), "map_color": "#AABBCC"}
    created = await client.post("/parcels", json=body)
    assert created.status_code == 200, created.text
    parcel_id = created.json()["id"]
    assert created.json()["map_color"] == "#aabbcc"
    original_geometry = created.json()["geojson"]
    updated = await client.put(f"/parcels/{parcel_id}", json={**body, "map_color":"#6366f1"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["geojson"] == original_geometry
    assert updated.json()["map_color"] == "#6366f1"
    reused = await client.post("/parcels", json=payload(crop.id))
    assert reused.status_code == 200
    assert reused.json()["id"] == parcel_id
    assert reused.json()["map_color"] == "#6366f1", "Clients omitting color must preserve it"
    rows = (await client.get("/parcels")).json()["items"]
    assert len(rows) == 1 and rows[0]["map_color"] == "#6366f1"
    rejected = await client.put(f"/parcels/{parcel_id}", json={**body,"map_color":"red; background:url(fake)"})
    assert rejected.status_code == 422


async def test_deleted_parcels_free_browser_quota_including_cms_deletions(setup):
    import copy
    db, user, crop, station, client = setup
    base = payload(crop.id)
    created = []
    for offset in (0, 0.001, 0.002):
        item = copy.deepcopy(base)
        item['geojson']['coordinates'][0] = [[x + offset, y] for x, y in base['geojson']['coordinates'][0]]
        response = await client.post('/parcels', json=item)
        assert response.status_code == 200
        created.append(response.json())
    assert (await client.get('/parcels')).json()['session_quota'] == {'count': 3, 'limit': 3}
    assert (await client.delete(f"/parcels/{created[0]['id']}")).status_code == 200
    assert (await client.get('/parcels')).json()['session_quota']['count'] == 2
    # Recreating a deleted geometry makes one new parcel and uses the freed place.
    replacement = await client.post('/parcels', json=base)
    assert replacement.status_code == 200
    assert replacement.json()['id'] != created[0]['id']
    assert (await client.get('/parcels')).json()['session_quota']['count'] == 3
    # CMS soft deletions have exactly the same effect; no browser history counts.
    from uuid import UUID
    for parcel in [created[1], created[2], replacement.json()]:
        row = await db.get(Plot, UUID(parcel['id']))
        row.deleted_at = datetime.now(UTC)
        row.active = False
    await db.commit()
    result = (await client.get('/parcels')).json()
    assert result['items'] == []
    assert result['session_quota']['count'] == 0
    assert (await client.post('/parcels', json=base)).status_code == 200


async def test_browser_quota_counts_existing_parcels_across_users_without_exposing_their_list(setup):
    db, user, crop, station, client = setup
    assert (await client.post('/parcels', json=payload(crop.id))).status_code == 200
    other = User(email=f'{uuid4().hex}@test.local', name='Otro productor', role_id=user.role_id,
                 password_hash='test-only', active=True)
    db.add(other)
    await db.flush()
    async def second_user(): return other
    client._transport.app.dependency_overrides[get_current_user] = second_user
    response = (await client.get('/parcels')).json()
    assert response['items'] == []
    assert response['session_quota'] == {'count': 1, 'limit': 3}
    assert (await client.get('/parcels', headers={'X-Parcel-Session': 'invalid'})).status_code == 422


async def test_open_portal_has_full_workflow_without_account_and_isolates_browsers(setup, monkeypatch):
    from app.api.endpoints.public_parcels import router as public_router
    from app.api.endpoints.resources import _plot_dict
    from app.db.models import PlotConversation
    import copy
    db, user, crop, station, private_client = setup
    app = private_client._transport.app
    app.include_router(public_router)
    from fastapi import HTTPException
    async def no_account():
        raise HTTPException(401, 'No administrative session')
    app.dependency_overrides[get_current_user] = no_account
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://public.test') as client:
        # No access/refresh/CSRF cookie and no user is created by this handshake.
        bootstrap_id = str(uuid4())
        response = await client.post('/public/parcels/browser-session', headers={'X-Parcel-Session': bootstrap_id})
        assert response.status_code == 200
        session = response.json()
        assert session['browser_id'] != bootstrap_id
        headers = {'X-Parcel-Session': session['browser_id'], 'X-Parcel-Access': session['access_token'], 'X-Chat-Browser': str(uuid4())}
        client.headers.update(headers)
        assert (await client.get('/parcels')).status_code == 401
        assert (await client.get('/public/parcels')).json()['items'] == []
        first = await client.post('/public/parcels', json=payload(crop.id))
        assert first.status_code == 200, first.text
        parcel = first.json()
        assert parcel['created_by'] is None
        assert parcel['browser_session_id'] == session['browser_id']
        # This is a real saved Plot visible to the CMS, without a synthetic user.
        from uuid import UUID
        row = await db.get(Plot, UUID(parcel['id']))
        assert (await _plot_dict(db, row))['crop_name'] == 'Trigo'
        assert (await client.post('/public/parcels', json=payload(crop.id))).json()['id'] == parcel['id']
        result = await client.post(f"/public/parcels/{parcel['id']}/analyses", json={'window_hours': 72})
        assert result.status_code == 200, result.text
        analysis = result.json()
        assert analysis['context']['selected_window_hours'] == 72
        assert (await client.get(f"/public/parcels/{parcel['id']}/analyses/latest")).json()['id'] == analysis['id']
        assert (await client.get(f"/public/parcels/{parcel['id']}/satellite/history")).status_code == 200
        report = await client.post(f"/public/parcels/analyses/{analysis['id']}/reports")
        assert report.status_code == 200, report.text
        async def interpret(*args, **kwargs):
            return {'markdown': 'Revise su parcela.', 'status': 'completed', 'images_processed': False}
        monkeypatch.setattr('app.api.endpoints.parcels.interpret_plot', interpret)
        assert (await client.post(f"/public/parcels/analyses/{analysis['id']}/interpret", json={'purpose':'ORCHESTRATOR','window_hours':72})).status_code == 200
        chat = await client.post(f"/public/parcels/analyses/{analysis['id']}/chat", data={'message':'¿Qué reviso?', 'window_hours':'72'})
        assert chat.status_code == 200, chat.text
        assert len(chat.json()['messages']) == 2
        saved = await db.scalar(select(PlotConversation).where(PlotConversation.analysis_id == UUID(analysis['id'])))
        assert saved.user_id is None
        # Images and documents use the public media path and stay browser-scoped.
        upload = await client.post(f"/public/parcels/analyses/{analysis['id']}/chat", data={'message':'Documento', 'window_hours':'72'}, files={'attachments': ('campo.txt', b'observacion del campo', 'text/plain')})
        assert upload.status_code == 200, upload.text
        url = upload.json()['messages'][-2]['attachments'][0]['url']
        assert url.startswith('/public/parcels/')
        client.cookies.set('fecoclima_chat_browser', headers['X-Chat-Browser'])
        assert (await client.get(url)).status_code == 200
        # Knowing the browser UUID alone cannot claim this session or its plots.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://public.test') as stranger:
            other = (await stranger.post('/public/parcels/browser-session', headers={'X-Parcel-Session':session['browser_id']})).json()
            assert other['browser_id'] != session['browser_id']
            stranger.headers.update({'X-Parcel-Session':other['browser_id'], 'X-Parcel-Access':other['access_token'], 'X-Chat-Browser':str(uuid4())})
            assert (await stranger.get('/public/parcels')).json()['items'] == []
            for path in (f"/{parcel['id']}/analyses/latest", f"/analyses/{analysis['id']}/chat", f"/{parcel['id']}/satellite/history"):
                assert (await stranger.get('/public/parcels' + path)).status_code == 404
            assert (await stranger.get(url)).status_code == 404
            assert (await stranger.delete(f"/public/parcels/{parcel['id']}")).status_code == 404
        for offset in (0.001,0.002):
            item = copy.deepcopy(payload(crop.id))
            item['geojson']['coordinates'][0] = [[x+offset,y] for x,y in item['geojson']['coordinates'][0]]
            assert (await client.post('/public/parcels', json=item)).status_code == 200
        item['geojson']['coordinates'][0] = [[x+0.01,y] for x,y in item['geojson']['coordinates'][0]]
        assert (await client.post('/public/parcels', json=item)).status_code == 409
        assert (await client.delete(f"/public/parcels/{parcel['id']}")).status_code == 200
        assert (await client.post('/public/parcels', json=item)).status_code == 200
        # Cookies alone never authorize a public mutation.
        client.headers.pop('X-Parcel-Session')
        assert (await client.post('/public/parcels', json=item)).status_code == 401
        client.headers.update(headers)
        again = (await client.post('/public/parcels/browser-session')).json()
        assert again['browser_id'] == session['browser_id']
        assert settings.access_cookie_name not in client.cookies
        assert settings.refresh_cookie_name not in client.cookies
