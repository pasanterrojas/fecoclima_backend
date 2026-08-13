from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import (
    AgentKnowledgeDocument,
    AIAgent,
    AIAgentVersion,
    BaseCondition,
    Crop,
    CropRuleSet,
    CropRuleVersion,
    DiseaseRule,
    IntegrationConfig,
    KnowledgeDocument,
    PhenologyStage,
    Plot,
    Role,
    Station,
    User,
    WeatherReading,
)
from app.services.agent_defaults import AGENT_PURPOSES, AI_OUTPUT_SCHEMA, default_agent_prompt
from app.services.weather import aggregate_range, evaluate_alert_for_day

logger = structlog.get_logger(__name__)

ROLES = {
    "superadmin": ["*"],
    "administrador": ["users:*", "stations:*", "rules:*", "reports:*", "integrations:*"],
    "analista": ["stations:read", "alerts:*", "reports:*", "knowledge:*"],
    "consulta": ["dashboard:read", "reports:read"],
}

SOY_STAGES = [
    ("SIEMBRA", "Siembra", 0), ("VE", "Emergencia", 5), ("V1", "Primera hoja unifoliada", 9),
    ("V2-V4", "Desarrollo vegetativo V2 a V4", 16), ("V5-V8", "Desarrollo vegetativo V5 a V8", 14),
    ("V9", "Desarrollo vegetativo V9", 10), ("R1", "Inicio de floración", 5), ("R3", "Inicio de vainas", 21),
    ("R5", "Inicio de llenado de granos", 17), ("R7", "Inicio de maduración", 11),
    ("R8", "Madurez fisiológica", 12), ("R9", "Cosecha", 7),
]
WHEAT_STAGES = [
    ("SIEMBRA", "Siembra", 0), ("GERMINACION", "Germinación", 17), ("BROTACION", "Brotación", 20),
    ("ESPIGAMIENTO", "Espigamiento", 25), ("FLORACION", "Floración", 17),
    ("LLENADO", "Llenado de granos", 32), ("MADURACION", "Maduración", 12), ("COSECHA", "Cosecha", 7),
]

SOY_CONDITIONS = [
    ("temperature", "Temperatura entre 20 y 30 °C", "temperature_avg_c", "between", 20, 30, None, "°C"),
    ("humidity", "Humedad relativa mayor a 80%", "humidity_avg_pct", "gt", None, None, 80, "%"),
    ("rain", "Lluvia diaria mayor a 0,1 mm", "rain_sum_mm", "gt", None, None, 0.1, "mm"),
    ("soil_temperature", "Temperatura del suelo entre 15 y 25 °C", "soil_temperature_avg_c", "between", 15, 25, None, "°C"),
    ("leaf_wetness", "Mojado foliar igual o superior a 6 horas", "leaf_wetness_hours", "gte", None, None, 6, "h"),
    ("soil_moisture", "Humedad del suelo igual o superior a 50%", "soil_moisture_avg_pct", "gte", None, None, 50, "%"),
]
WHEAT_CONDITIONS = [
    ("temperature", "Temperatura entre 18 y 25 °C", "temperature_avg_c", "between", 18, 25, None, "°C"),
    ("humidity", "Humedad relativa mayor a 70%", "humidity_avg_pct", "gt", None, None, 70, "%"),
    ("rain", "Lluvia diaria mayor a 0,1 mm", "rain_sum_mm", "gt", None, None, 0.1, "mm"),
    ("soil_temperature", "Temperatura del suelo entre 15 y 25 °C", "soil_temperature_avg_c", "between", 15, 25, None, "°C"),
    ("leaf_wetness", "Mojado foliar igual o superior a 6 horas", "leaf_wetness_hours", "gte", None, None, 6, "h"),
    ("soil_moisture", "Humedad del suelo igual o superior a 50%", "soil_moisture_avg_pct", "gte", None, None, 50, "%"),
]


async def _role(db: AsyncSession, code: str) -> Role:
    role = await db.scalar(select(Role).where(Role.code == code))
    if not role:
        role = Role(code=code, name=code.replace("_", " ").title(), permissions={"allowed": ROLES[code]})
        db.add(role)
        await db.flush()
    return role


async def _crop(db: AsyncSession, code: str, name: str, status: str, description: str) -> Crop:
    crop = await db.scalar(select(Crop).where(Crop.code == code))
    if not crop:
        crop = Crop(code=code, name=name, rules_status=status, description=description)
        db.add(crop)
        await db.flush()
    return crop


async def _seed_stages(db: AsyncSession, crop: Crop, data: list[tuple[str, str, int]]) -> None:
    start = 0
    for order, (code, name, duration) in enumerate(data):
        end = start if duration == 0 else start + duration - 1
        stage = await db.scalar(select(PhenologyStage).where(PhenologyStage.crop_id == crop.id, PhenologyStage.code == code, PhenologyStage.cycle_type == "normal"))
        if not stage:
            db.add(PhenologyStage(crop_id=crop.id, code=code, name=name, description=f"Etapa {name} estimada a partir de la fecha de siembra.", order_index=order, duration_days=duration, start_day=start, end_day=end, cycle_type="normal"))
        start = end + 1


async def _seed_rule(db: AsyncSession, crop: Crop, version: str, conditions_data, disease_data: list[dict]) -> CropRuleVersion:
    rule = await db.scalar(select(CropRuleVersion).where(CropRuleVersion.crop_id == crop.id, CropRuleVersion.version == version))
    if rule:
        return rule
    rule_set = await db.scalar(select(CropRuleSet).where(CropRuleSet.crop_id == crop.id))
    if not rule_set:
        rule_set = CropRuleSet(crop_id=crop.id, name=f"Reglas base {crop.name}", description="Reglas determinísticas versionadas.")
        db.add(rule_set)
        await db.flush()
    rule = CropRuleVersion(rule_set_id=rule_set.id, crop_id=crop.id, name=f"Regla base {crop.name}", description="Seis condiciones base para evaluación diaria.", version=version, status="PUBLISHED", effective_from=date(2026, 1, 1), configuration={"moderate_min": 3, "severe_count": 6, "window_hours": 72}, substitution_rules={})
    db.add(rule)
    await db.flush()
    for order, (key, label, variable, operator, minimum, maximum, value, unit) in enumerate(conditions_data, start=1):
        db.add(BaseCondition(rule_version_id=rule.id, key=key, label=label, variable=variable, operator=operator, threshold_min=minimum, threshold_max=maximum, threshold_value=value, unit=unit, aggregation="daily", required=True, order_index=order))
    for disease in disease_data:
        db.add(DiseaseRule(rule_version_id=rule.id, name=disease["name"], causal_agent=disease.get("agent"), conditions=disease.get("conditions", {}), valid_stages=disease.get("stages", []), validation_status=disease.get("validation_status", "PENDING_TECHNICAL"), source_reference=disease.get("source"), enabled_for_evaluation=disease.get("enabled", False)))
    return rule


SOY_DISEASES = [
    {"name": "Roya de la soja", "agent": "Phakopsora pachyrhizi", "conditions": {"all": [{"variable": "leaf_wetness_hours", "operator": "between", "min": 6, "max": 8, "unit": "h"}, {"variable": "temperature_avg_c", "operator": "between", "min": 15, "max": 28, "unit": "°C"}]}, "stages": [], "validation_status": "PUBLISHED", "enabled": True, "source": "Plantilla Enfermedades Soja: 6-8 h de mojado y 15-28 °C."},
    {"name": "Oidio", "agent": "Microsphaera diffusa", "conditions": {"all": [{"variable": "temperature_avg_c", "operator": "between", "min": 18, "max": 26, "unit": "°C"}]}, "stages": [], "validation_status": "PUBLISHED", "enabled": True, "source": "Plantilla Enfermedades Soja: 18-26 °C; por encima de 30 °C se detiene."},
    {"name": "Mancha marrón o Septoria", "agent": "Septoria glycines", "conditions": {"all": [{"variable": "temperature_avg_c", "operator": "between", "min": 25, "max": 30, "unit": "°C"}, {"variable": "leaf_wetness_hours", "operator": "between", "min": 6, "max": 36, "unit": "h"}]}, "stages": ["R5", "R6"], "validation_status": "PUBLISHED", "enabled": True, "source": "Plantilla Enfermedades Soja: 25-30 °C, mojado 6-36 h, R5-R6."},
    {"name": "Mancha anillada", "agent": "Corynespora cassiicola", "conditions": {"all": [{"variable": "humidity_avg_pct", "operator": "gte", "value": 80, "unit": "%"}, {"variable": "temperature_avg_c", "operator": "between", "min": 23, "max": 39, "unit": "°C"}]}, "stages": [], "validation_status": "PUBLISHED", "enabled": True, "source": "Plantilla Enfermedades Soja: humedad ≥80% y 23-39 °C."},
    {"name": "Mildiu", "agent": "Peronospora manshurica", "validation_status": "PENDING_TECHNICAL", "source": "‘Humedad elevada’ requiere umbral validado."},
    {"name": "Mancha ojo de rana", "agent": "Cercospora sojina", "stages": ["V6", "V7", "V8", "V9", "R1", "R3", "R5", "R6", "R7"], "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ y ‘temperatura cercana a 26 °C’ requieren tolerancia validada."},
    {"name": "Tizón de la hoja y mancha púrpura", "agent": "Cercospora kikuchii", "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ requiere umbral validado."},
    {"name": "Antracnosis", "agent": "Colletotrichum truncatum", "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ requiere umbral validado."},
    {"name": "Cancro del tallo", "agent": "Diaporthe phaseolorum", "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ requiere umbral validado."},
    {"name": "Pudrición carbonosa", "agent": "Macrophomina phaseolina", "validation_status": "PENDING_TECHNICAL", "source": "El estrés hídrico debe convertirse en una regla medible."},
    {"name": "Síndrome de muerte súbita", "agent": "Fusarium spp.", "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ requiere umbral validado."},
    {"name": "Damping off", "agent": "Complejo de patógenos de suelo", "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ requiere umbral validado."},
    {"name": "Tizón y pústula bacteriana", "agent": "Pseudomonas / Xanthomonas", "validation_status": "PENDING_TECHNICAL", "source": "‘Lluvias constantes’ requiere ventana y acumulado validados."},
]
WHEAT_DISEASES = [
    {"name": "Roya del trigo", "agent": "Puccinia triticina", "conditions": {"all": [{"variable": "temperature_avg_c", "operator": "between", "min": 15, "max": 25, "unit": "°C"}]}, "stages": [], "validation_status": "PUBLISHED", "enabled": True, "source": "Plantilla Enfermedades Trigo: 15-25 °C."},
    {"name": "Oidio", "agent": "Blumeria graminis f. sp. tritici", "validation_status": "PENDING_TECHNICAL", "source": "Debe mapearse ‘elongación del tallo’ al calendario fenológico publicado."},
    {"name": "Mancha amarilla", "agent": "Drechslera tritici-repentis", "validation_status": "PENDING_TECHNICAL", "source": "La presencia de hoja bandera requiere observación de campo."},
    {"name": "Mancha marrón", "agent": "Bipolaris sorokiniana", "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ requiere umbral validado."},
    {"name": "Fusariosis", "agent": "Fusarium graminearum", "validation_status": "PENDING_TECHNICAL", "source": "‘Lluvias persistentes’ requiere ventana y acumulado validados."},
    {"name": "Bruzone", "agent": "Pyricularia oryzae", "validation_status": "PENDING_TECHNICAL", "source": "‘Alta humedad’ requiere umbral validado."},
]


async def _seed_agent(db: AsyncSession, crop: Crop, purpose: str) -> None:
    suffix = {
        "CROP_ANALYSIS": "GENERAL",
        "SATELLITE_ANALYSIS": "SATELLITE",
        "PHENOLOGY_ANALYSIS": "PHENOLOGY",
        "YEAR_COMPARISON": "YEAR_COMPARE",
        "DAILY_DIGEST": "DAILY",
    }[purpose]
    if purpose == "CROP_ANALYSIS":
        agent = await db.scalar(
            select(AIAgent).where(
                AIAgent.crop_id == crop.id,
                AIAgent.purpose.in_(["CROP_ANALYSIS", "climate_report"]),
                AIAgent.deleted_at.is_(None),
            ).order_by(AIAgent.created_at)
        )
        if agent:
            agent.purpose = "CROP_ANALYSIS"
            agent.active = True
            version = await db.scalar(
                select(AIAgentVersion)
                .where(AIAgentVersion.agent_id == agent.id, AIAgentVersion.active.is_(True))
                .order_by(AIAgentVersion.created_at.desc())
                .limit(1)
            )
            if not version:
                db.add(AIAgentVersion(
                    agent_id=agent.id,
                    version=agent.current_version or "1.0.0",
                    system_prompt=default_agent_prompt(crop.name, purpose),
                    model=settings.ai_default_model,
                    temperature=0.2,
                    max_tokens=1500,
                    output_schema=AI_OUTPUT_SCHEMA,
                    rag_collection=crop.code,
                    active=True,
                ))
            return
    agent = await db.scalar(select(AIAgent).where(
        AIAgent.crop_id == crop.id,
        AIAgent.purpose == purpose,
        AIAgent.deleted_at.is_(None),
    ))
    if agent:
        return
    agent = AIAgent(
        name=f"{AGENT_PURPOSES[purpose]} · {crop.name}",
        code=f"AGENT_{crop.code}_{suffix}",
        crop_id=crop.id,
        purpose=purpose,
        description=AGENT_PURPOSES[purpose],
        current_version="1.0.0",
        active=True,
    )
    db.add(agent)
    await db.flush()
    db.add(AIAgentVersion(
        agent_id=agent.id,
        version="1.0.0",
        system_prompt=default_agent_prompt(crop.name, purpose),
        model=settings.ai_default_model,
        temperature=0.2,
        max_tokens=1800 if purpose == "SATELLITE_ANALYSIS" else 1500,
        output_schema=AI_OUTPUT_SCHEMA,
        rag_collection=crop.code,
        active=True,
    ))
    existing_documents = list(
        (
            await db.scalars(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.crop_id == crop.id,
                    KnowledgeDocument.active.is_(True),
                    KnowledgeDocument.deleted_at.is_(None),
                )
            )
        ).all()
    )
    for document in existing_documents:
        db.add(AgentKnowledgeDocument(agent_id=agent.id, document_id=document.id))


async def _link_document_to_crop_agents(db: AsyncSession, document: KnowledgeDocument) -> None:
    """Link a seeded crop document to every active agent for that crop.

    The migration backfills existing deployments, while this helper covers clean
    installations where agents are seeded before the bundled documents.
    """
    if document.crop_id is None or document.deleted_at is not None or not document.active:
        return
    agents = list(
        (
            await db.scalars(
                select(AIAgent).where(
                    AIAgent.crop_id == document.crop_id,
                    AIAgent.deleted_at.is_(None),
                )
            )
        ).all()
    )
    if not agents:
        return
    linked_agent_ids = set(
        (
            await db.scalars(
                select(AgentKnowledgeDocument.agent_id).where(
                    AgentKnowledgeDocument.document_id == document.id
                )
            )
        ).all()
    )
    for agent in agents:
        if agent.id not in linked_agent_ids:
            db.add(AgentKnowledgeDocument(agent_id=agent.id, document_id=document.id))


async def seed_all(db: AsyncSession) -> None:
    for code in ROLES:
        await _role(db, code)
    await db.commit()
    user_count = len((await db.scalars(select(User.id))).all())
    if user_count == 0:
        role = await db.scalar(select(Role).where(Role.code == "superadmin"))
        db.add(User(email=settings.superadmin_email.lower(), name=settings.superadmin_name, password_hash=hash_password(settings.superadmin_password), role_id=role.id, active=True))
        logger.info("initial_superadmin_created", email=settings.superadmin_email)
    soy = await _crop(db, "SOJA", "Soja", "active", "Cultivo con reglas base y enfermedades iniciales versionadas.")
    wheat = await _crop(db, "TRIGO", "Trigo", "active", "Cultivo con reglas base y enfermedades iniciales versionadas.")
    corn = await _crop(db, "MAIZ", "Maíz", "pending", "Cultivo disponible para meteorología general. Reglas agronómicas pendientes de validación.")
    await _seed_stages(db, soy, SOY_STAGES)
    await _seed_stages(db, wheat, WHEAT_STAGES)
    await _seed_rule(db, soy, "1.0.0", SOY_CONDITIONS, SOY_DISEASES)
    await _seed_rule(db, wheat, "1.0.0", WHEAT_CONDITIONS, WHEAT_DISEASES)
    for crop in (soy, wheat, corn):
        for purpose in AGENT_PURPOSES:
            await _seed_agent(db, crop, purpose)
    station = await db.scalar(select(Station).where(Station.code == "RAUL-PENA"))
    if not station:
        station = Station(
            name="Raul Peña",
            code="RAUL-PENA",
            external_guid="C2694D32-5A4B-48C0-A3B6-8345D9E4935B",
            country="Paraguay",
            department="Alto Paraná",
            district="Doctor Raúl Peña",
            locality="Doctor Raúl Peña",
            latitude=-26.15851,
            longitude=-55.27218,
            altitude_m=364,
            timezone="America/Asuncion",
            source_type="FECOCLIMA_API",
            expected_interval_minutes=10,
            integration_config={
                "header_name": "clima-key",
                "range_max_months": 6,
                "soil_moisture_unit": "cb",
                "coordinate_source": "locality_centroid_approximation",
                "coordinate_note": "Actualizar con la coordenada exacta de la estación cuando esté disponible.",
            },
            leaf_sensor_config={
                "mode": "threshold",
                "wet_threshold": 6,
                "scale_min": 0,
                "scale_max": 15,
                "interpretation": "samples_at_or_above_threshold_count_as_wet",
            },
            available_variables=["temp", "chill", "hIndex", "humid", "dewpt", "wind", "hiWind", "windDir", "rain", "totalRain", "barom", "solar", "et", "uv", "temperaturaSuelo", "humedadSuelo", "sensorHoja"],
        )
        db.add(station)
    else:
        station.department = station.department or "Alto Paraná"
        station.district = station.district or "Doctor Raúl Peña"
        station.locality = station.locality or "Doctor Raúl Peña"
        station.latitude = station.latitude if station.latitude is not None else -26.15851
        station.longitude = station.longitude if station.longitude is not None else -55.27218
        station.altitude_m = station.altitude_m if station.altitude_m is not None else 364
        integration_config = dict(station.integration_config or {})
        integration_config.setdefault("header_name", "clima-key")
        integration_config.setdefault("range_max_months", 6)
        integration_config.setdefault("soil_moisture_unit", "cb")
        integration_config.setdefault("coordinate_source", "locality_centroid_approximation")
        integration_config.setdefault("coordinate_note", "Actualizar con la coordenada exacta de la estación cuando esté disponible.")
        station.integration_config = integration_config
        if (station.leaf_sensor_config or {}).get("mode") in {None, "", "unknown"}:
            station.leaf_sensor_config = {
                "mode": "threshold",
                "wet_threshold": 6,
                "scale_min": 0,
                "scale_max": 15,
                "interpretation": "samples_at_or_above_threshold_count_as_wet",
            }
    for config in [
        ("API FECOCLIMA", "WEATHER", "FECOCLIMA", settings.fecoclima_base_url),
        ("Pronóstico Open-Meteo", "FORECAST", "OPEN_METEO", settings.open_meteo_base_url),
        ("Sentinel-2", "SATELLITE", "COPERNICUS_DATA_SPACE", settings.copernicus_stac_search_url),
        ("Proveedor IA", "AI", "OPENAI_COMPATIBLE", settings.ai_base_url),
        ("Correo SMTP", "SMTP", "SMTP", None),
        ("Avisos diarios a productores", "NOTIFICATION", "EMAIL_TELEGRAM_DIGEST", None),
    ]:
        existing = await db.scalar(select(IntegrationConfig).where(IntegrationConfig.name == config[0]))
        if not existing:
            db.add(IntegrationConfig(name=config[0], integration_type=config[1], provider=config[2], base_url=config[3], active=bool(config[3]) if config[1] not in {"WEATHER", "SMTP"} else False, non_sensitive_config={}))
    source_docs = [
        ("Especificaciones técnicas API FECOCLIMA", None, "Federación de Cooperativas de Producción Ltda.", 2026, "La API FECOCLIMA consulta lecturas por GUID, año, mes inicial y mes final. Cada solicitud puede cubrir como máximo seis meses. La respuesta incluye nombreEstacion, fechaMuestreo, temperatura, sensación térmica, índice de calor, humedad, punto de rocío, viento, ráfaga, dirección, lluvia, lluvia acumulada, presión, radiación solar, evapotranspiración, UV, temperaturas mínima y máxima, temperatura de suelo, humedad de suelo y sensor de hoja. La autenticación usa un encabezado secreto que debe permanecer solo en el backend."),
        ("Plantilla técnica de fenología y enfermedades de soja", soy.id, "Fuente técnica suministrada", 2026, "Fenología normal de soja: Siembra, VE, V1, V2-V4, V5-V8, V9, R1, R3, R5, R7, R8 y R9. Roya de la soja: 6 a 8 horas de mojado foliar y 15 a 28 °C. Oidio: 18 a 26 °C y la infección se detiene por encima de 30 °C. Mancha marrón: 25 a 30 °C y mojado de 6 a 36 horas, especialmente R5-R6. Mancha anillada: humedad al menos 80% y 23 a 39 °C. Las expresiones alta humedad, lluvias constantes y estrés hídrico requieren validación técnica antes de convertirse en umbral."),
        ("Plantilla técnica de fenología y enfermedades de trigo", wheat.id, "Fuente técnica suministrada", 2026, "Fenología normal de trigo: Siembra, Germinación, Brotación, Espigamiento, Floración, Llenado de granos, Maduración y Cosecha. Roya: 15 a 25 °C. Oidio: 10 a 22 °C a partir de elongación del tallo. Mancha amarilla requiere hoja bandera y encañado. Fusariosis se asocia con lluvias durante floración. Las expresiones alta humedad y lluvias persistentes deben medirse y validarse antes de publicar reglas."),
    ]
    for title, crop_id, institution, year, text in source_docs:
        existing = await db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.title == title))
        if not existing:
            existing = KnowledgeDocument(
                title=title,
                crop_id=crop_id,
                institution=institution,
                year=year,
                version="1.0",
                source_text=text,
                index_status="PENDING",
                active=True,
            )
            db.add(existing)
            await db.flush()
        await _link_document_to_crop_agents(db, existing)
    await db.commit()
    if settings.load_demo_data:
        await seed_demo(db, soy, wheat)


async def seed_demo(db: AsyncSession, soy: Crop, wheat: Crop) -> None:
    station = await db.scalar(select(Station).where(Station.code == "DEMO-ASUNCION"))
    if not station:
        station = Station(name="Demo Asunción", code="DEMO-ASUNCION", description="Estación ficticia con datos sintéticos para pruebas. No usar para decisiones reales.", country="Paraguay", department="Capital", locality="Asunción", latitude=-25.2637, longitude=-57.5759, timezone="America/Asuncion", source_type="MANUAL", expected_interval_minutes=30, leaf_sensor_config={"mode": "binary_wet", "wet_value": 1}, available_variables=["temperature_c", "humidity_pct", "rain_mm", "soil_temperature_c", "soil_moisture_pct", "leaf_sensor_value"])
        db.add(station)
        await db.flush()
    plot = await db.scalar(select(Plot).where(Plot.name == "Parcela demostrativa de soja"))
    if not plot:
        plot = Plot(name="Parcela demostrativa de soja", producer_name="Datos ficticios", crop_id=soy.id, station_id=station.id, area_ha=5, latitude=station.latitude, longitude=station.longitude, influence_radius_m=500)
        db.add(plot)
        await db.flush()
    demo_planting_date = date.today() - timedelta(days=64)
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=96)
    for index in range(193):
        sampled = start + timedelta(minutes=30 * index)
        hour = sampled.hour
        temp = 23 + 5 * math.sin((hour - 7) / 24 * 2 * math.pi)
        humidity = 82 - 18 * math.sin((hour - 7) / 24 * 2 * math.pi)
        rain = 0.4 if index % 53 in {0, 1, 2} else 0.0
        leaf = 1 if hour >= 21 or hour <= 7 else 0
        statement = insert(WeatherReading).values(station_id=station.id, sampled_at=sampled, source="estimated", source_reference="DEMO_SYNTHETIC", temperature_c=round(temp, 1), feels_like_c=round(temp + 0.8, 1), humidity_pct=round(max(45, min(98, humidity)), 1), dew_point_c=round(temp - 2.5, 1), wind_speed_ms=2.4, wind_gust_ms=4.1, wind_direction_deg=120, rain_mm=rain, total_rain_mm=rain, pressure_hpa=1008, solar_wm2=max(0, round(620 * math.sin(max(0, min(math.pi, (hour - 6) / 12 * math.pi))))), evapotranspiration_mm=0.05 if 7 <= hour <= 18 else 0, uv_index=4 if 10 <= hour <= 15 else 0, soil_temperature_c=21.8, soil_moisture_pct=61.0, leaf_sensor_value=leaf, raw_payload={"demo": True}, quality_flags=[]).on_conflict_do_nothing(index_elements=[WeatherReading.station_id, WeatherReading.sampled_at])
        await db.execute(statement)
    await db.commit()
    date_from = (datetime.now(UTC) - timedelta(days=4)).date()
    date_to = datetime.now(UTC).date()
    await aggregate_range(db, station.id, date_from, date_to)
    current = date_from
    while current <= date_to:
        await evaluate_alert_for_day(db, station.id, soy.id, current, planting_date=demo_planting_date)
        await evaluate_alert_for_day(db, station.id, wheat.id, current, planting_date=demo_planting_date)
        current += timedelta(days=1)
