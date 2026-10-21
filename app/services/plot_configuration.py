from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Crop, Plot, Station
from app.schemas.resources import PlotPayload
from app.services.geospatial import parcel_metrics, sync_plot_influences


async def configure_plot(db: AsyncSession, plot: Plot, payload: PlotPayload) -> None:
    if payload.crop_id:
        crop = await db.get(Crop, payload.crop_id)
        if not crop or not crop.active or crop.deleted_at:
            raise ValueError("Seleccione un cultivo activo del catálogo")
    for key, value in payload.model_dump().items():
        if key == "map_color" and key not in payload.model_fields_set:
            continue  # Older clients/re-analysis must preserve the chosen color.
        setattr(plot, key, value)
    if plot.geojson:
        from app.services.parcel_identity import geometry_key
        plot.geometry_key = geometry_key(plot.geojson)
        for key, value in parcel_metrics(plot.geojson).items():
            setattr(plot, key, value)
        await db.flush()
        await sync_plot_influences(db, plot)
    elif plot.station_id:
        station = await db.get(Station, plot.station_id)
        if not station or not station.active or station.deleted_at:
            raise ValueError("Estación inválida")
    await db.flush()
    await db.refresh(plot)
