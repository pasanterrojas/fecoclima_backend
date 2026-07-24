"""Operational forecast, leaf-sensor semantics and satellite defaults.

Revision ID: 0003_forecast_satellite
Revises: 0002_planting_date_context
Create Date: 2026-07-23
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0003_forecast_satellite"
down_revision = "0002_planting_date_context"
branch_labels = None
depends_on = None


def _json(value: dict) -> str:
    """Serialize migration JSON without embedding it directly in SQL text."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def upgrade() -> None:
    connection = op.get_bind()

    # IMPORTANT:
    # JSON documents are passed as bind parameters. Embedding JSON such as
    # '"wet_threshold":6' directly in sa.text() makes SQLAlchemy interpret
    # ':6' as a bind parameter and abort the migration.
    connection.execute(
        sa.text(
            """
            UPDATE stations
            SET department = COALESCE(department, 'Alto Paraná'),
                district = COALESCE(district, 'Doctor Raúl Peña'),
                locality = COALESCE(locality, 'Doctor Raúl Peña'),
                latitude = COALESCE(latitude, -26.15851),
                longitude = COALESCE(longitude, -55.27218),
                altitude_m = COALESCE(altitude_m, 364),
                integration_config = COALESCE(integration_config, CAST('{}' AS jsonb))
                    || CAST(:station_integration_patch AS jsonb),
                leaf_sensor_config = CASE
                    WHEN leaf_sensor_config IS NULL
                      OR leaf_sensor_config = CAST('{}' AS jsonb)
                      OR COALESCE(leaf_sensor_config->>'mode', 'unknown') = 'unknown'
                    THEN CAST(:leaf_sensor_config AS jsonb)
                    ELSE leaf_sensor_config
                END
            WHERE code = 'RAUL-PENA'
               OR upper(external_guid) = 'C2694D32-5A4B-48C0-A3B6-8345D9E4935B'
            """
        ),
        {
            "station_integration_patch": _json(
                {
                    "coordinate_source": "locality_centroid_approximation",
                    "coordinate_note": (
                        "Actualizar con la coordenada exacta de la estación "
                        "cuando esté disponible."
                    ),
                }
            ),
            "leaf_sensor_config": _json(
                {
                    "mode": "threshold",
                    "wet_threshold": 6,
                    "scale_min": 0,
                    "scale_max": 15,
                    "interpretation": "samples_at_or_above_threshold_count_as_wet",
                }
            ),
        },
    )

    connection.execute(
        sa.text(
            """
            UPDATE integration_configs
            SET active = TRUE,
                base_url = COALESCE(NULLIF(base_url, ''), :open_meteo_base_url),
                non_sensitive_config = COALESCE(non_sensitive_config, CAST('{}' AS jsonb))
                    || CAST(:open_meteo_config AS jsonb)
            WHERE provider = 'OPEN_METEO'
            """
        ),
        {
            "open_meteo_base_url": "https://api.open-meteo.com/v1",
            "open_meteo_config": _json(
                {
                    "forecast_days": 7,
                    "timezone": "America/Asuncion",
                    "hourly_soil": True,
                    "leaf_wetness_proxy": True,
                }
            ),
        },
    )

    connection.execute(
        sa.text(
            """
            UPDATE integration_configs
            SET active = TRUE,
                base_url = COALESCE(NULLIF(base_url, ''), :copernicus_stac_url),
                non_sensitive_config = COALESCE(non_sensitive_config, CAST('{}' AS jsonb))
                    || CAST(:copernicus_config AS jsonb)
            WHERE provider = 'COPERNICUS_DATA_SPACE'
            """
        ),
        {
            "copernicus_stac_url": "https://stac.dataspace.copernicus.eu/v1/search",
            "copernicus_config": _json(
                {
                    "stac_search_url": "https://stac.dataspace.copernicus.eu/v1/search",
                    "collection": "sentinel-2-l2a",
                    "max_cloud_percentage": 40,
                }
            ),
        },
    )


def downgrade() -> None:
    # Data corrections are intentionally preserved.
    pass
