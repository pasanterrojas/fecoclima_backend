from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pypdf import PdfReader

from app.db.models import Crop, Report, Station
from app.services import reporting


class ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_report_pdf_wraps_long_tables_and_renders_markdown(monkeypatch, tmp_path) -> None:
    report_id = uuid4()
    station_id = uuid4()
    crop_id = uuid4()
    evaluation_id = uuid4()
    report = SimpleNamespace(
        id=report_id,
        station_id=station_id,
        crop_id=crop_id,
        planting_date=date(2026, 5, 21),
        date_from=date(2026, 8, 6),
        date_to=date(2026, 8, 13),
        title="Reporte Trigo · Raul Peña",
        rule_version="1.0.0",
        status="PENDING",
        error_message=None,
        completed_at=None,
        summary={},
    )
    station = SimpleNamespace(
        id=station_id,
        name="Raul Peña",
        locality="Raul Peña",
        district="Tavapy",
        department="Alto Paraná",
    )
    crop = SimpleNamespace(id=crop_id, name="Trigo")
    start = date(2026, 8, 6)
    daily = [
        SimpleNamespace(
            summary_date=start + timedelta(days=index),
            records_count=140,
            completeness_pct=97.923,
            soil_moisture_avg_pct=115.25,
            leaf_wetness_hours=4.673,
            temperature_avg_c=17.234,
            humidity_avg_pct=82.456,
            rain_sum_mm=32.643 if index == 0 else 0.0,
        )
        for index in range(8)
    ]
    alerts = [
        SimpleNamespace(
            id=evaluation_id,
            evaluation_date=item.summary_date,
            level="MODERADA",
            conditions_met=5,
            conditions_total=6,
            stage_code="LLENADO",
            diseases=[{"name": "Roya del trigo con una descripción extensa para validar el ajuste de línea"}],
        )
        for item in daily
    ]
    forecasts = [
        SimpleNamespace(
            forecast_date=date(2026, 8, 13) + timedelta(days=index),
            temperature_min_c=15.234,
            temperature_max_c=25.678,
            precipitation_mm=14.632,
            precipitation_probability_pct=84.872,
            humidity_pct=91.239,
            wind_speed_ms=5.249,
            provider="OPEN_METEO_PROVEEDOR_CON_NOMBRE_EXTENSO",
        )
        for index in range(7)
    ]
    stage = SimpleNamespace(
        code="LLENADO",
        name="Llenado de granos",
        description="Etapa de llenado estimada.",
        order_index=1,
        start_day=70,
        end_day=110,
        active=True,
        deleted_at=None,
    )
    satellite = SimpleNamespace(
        captured_at=datetime(2026, 8, 12, 13, 47, tzinfo=UTC),
        provider="COPERNICUS_DATA_SPACE_SENTINEL_2_L2A",
        cloud_percentage=13.823,
        ndvi=0.59823,
        ndmi=0.14618,
        lai=2.341,
    )

    class FakeDb:
        def __init__(self):
            self.scalar_index = 0
            self.scalars_index = 0
            self.added = []

        async def get(self, model, identifier):
            if model is Report and identifier == report_id:
                return report
            if model is Station and identifier == station_id:
                return station
            if model is Crop and identifier == crop_id:
                return crop
            return None

        async def scalars(self, _query):
            groups = [daily, alerts, forecasts, [stage]]
            result = ScalarRows(groups[self.scalars_index])
            self.scalars_index += 1
            return result

        async def scalar(self, _query):
            values = [SimpleNamespace(id=uuid4()), satellite]
            result = values[self.scalar_index]
            self.scalar_index += 1
            return result

        async def commit(self):
            return None

        def add(self, value):
            self.added.append(value)

    async def fake_forecast_risk(*_args, **_kwargs):
        return {
            "days": [
                {
                    "date": item.forecast_date.isoformat(),
                    "stage_code": "LLENADO",
                    "conditions_met": 5,
                    "conditions_total": 6,
                    "coverage_pct": 97.923,
                    "potential_level": "MODERADA",
                }
                for item in forecasts
            ],
            "diseases": [
                {
                    "name": "Roya del trigo",
                    "risk": "moderado",
                    "dates": [item.forecast_date.isoformat() for item in forecasts],
                    "source": (
                        "Regla técnica publicada con una referencia extensa que debe ajustarse dentro de la celda "
                        "sin invadir las columnas contiguas ni salir del área imprimible del documento."
                    ),
                }
            ],
            "unresolved_diseases": [],
        }

    async def fake_recommendation(*_args, **_kwargs):
        return {
            "markdown": (
                "## Lo importante hoy\n\nLa humedad y el mojado foliar favorecen vigilancia.\n\n"
                "### Qué revisar\n- Recorra hojas superiores.\n- Registre fotografías georreferenciadas."
            ),
            "sources": [
                {"documento": "Guía técnica del cultivo de trigo", "pagina_o_seccion": "Manejo sanitario, páginas 18 a 22"}
            ],
            "_agent_status": "completed",
        }

    monkeypatch.setattr(reporting.settings, "storage_path", tmp_path)
    monkeypatch.setattr(reporting, "build_forecast_risk", fake_forecast_risk)
    monkeypatch.setattr(reporting, "run_agent_for_alert", fake_recommendation)

    result = await reporting.generate_report_pdf(FakeDb(), report_id)
    pdf_path = tmp_path / "reports" / result.filename
    assert pdf_path.is_file()
    reader = PdfReader(str(pdf_path))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Lo importante hoy" in extracted
    assert "13 al 19 ago. 2026" in extracted
    assert "Guía técnica del cultivo de trigo" in extracted
    assert report.status == "COMPLETED"


def test_report_formatters_use_one_decimal_and_compact_dates() -> None:
    assert reporting._format_decimal(17.234) == "17.2"
    assert reporting._format_decimal(97.923, suffix="%") == "97.9%"
    assert reporting._format_date_list(["2026-08-13", "2026-08-14", "2026-08-15"]) == "13 al 15 ago. 2026"
