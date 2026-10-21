"""Fixed client classes, shared by raster rendering and its API legend.

Source: docs/ANEXO_TECNICO_VISUALIZADOR_SATELITAL.pdf, pp. 1–2.
An exact boundary belongs to the lower class (<=), never both classes.
"""
from __future__ import annotations

from bisect import bisect_left
from math import isfinite
from typing import Any

VISUAL_VERSION = "client-annex-v1"
VEGETATION_COLORS = ("#FF2200", "#FF9900", "#FFFF00", "#7AAB00", "#006100")
WATER_COLORS = ("#A6611A", "#DFC27D", "#D9F0F3", "#80CDC1", "#018571")
# Keep the client's ascending color order, including MSI. A color class is not
# an agronomic diagnosis; MSI increases with moisture stress. NBR is vegetation.
INDEX_CLASSES: dict[str, dict[str, Any]] = {
    "ndvi": {"breaks": (0.30, 0.40, 0.50, 0.60), "colors": VEGETATION_COLORS},
    "ndmi": {"breaks": (-0.20, 0, 0.20, 0.40), "colors": WATER_COLORS,
             "formula": "(B08 − B11) / (B08 + B11) · NDWI Gao / NDMI"},
    "ndwi": {"breaks": (-0.50, -0.20, 0, 0.30), "colors": WATER_COLORS,
             "formula": "(B03 − B08) / (B03 + B08) · NDWI McFeeters"},
    "evi": {"breaks": (0.15, 0.30, 0.45, 0.60), "colors": VEGETATION_COLORS},
    "savi": {"breaks": (0.15, 0.30, 0.45, 0.60), "colors": VEGETATION_COLORS},
    "ndre": {"breaks": (0.10, 0.20, 0.30, 0.40), "colors": VEGETATION_COLORS},
    "gndvi": {"breaks": (0.20, 0.35, 0.50, 0.65), "colors": VEGETATION_COLORS},
    "nbr": {"breaks": (-0.20, 0, 0.20, 0.40), "colors": VEGETATION_COLORS},
    "recl": {"breaks": (1, 2, 3, 4), "colors": VEGETATION_COLORS,
             "formula": "B07 / B05 − 1 · clorofila de borde rojo (Sentinel Hub)",
             "source": "Anexo técnico del cliente, pp. 1–2; último corte >4 confirmado el 20/10/2026",
             "formula_source": "https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/chl_rededge/"},
    "msi": {"breaks": (0.40, 0.80, 1.20, 1.60), "colors": WATER_COLORS,
            "formula": "B11 / B08 · valores altos: mayor estrés hídrico"},
}


def classify_index(name: str, value: float | None) -> int | None:
    """Return a zero-based class; missing/invalid pixels have no class."""
    if value is None or not isfinite(value):
        return None
    return bisect_left(INDEX_CLASSES[name]["breaks"], value)


def index_legend(name: str) -> dict[str, Any]:
    config = INDEX_CLASSES[name]
    breaks = config["breaks"]

    def number(value: float) -> str:
        return f"{value:g}".replace("-", "−").replace(".", ",")

    classes = []
    for index, color in enumerate(config["colors"]):
        lower = breaks[index - 1] if index else None
        upper = breaks[index] if index < len(breaks) else None
        label = (f"≤ {number(upper)}" if lower is None else
                 f"> {number(lower)}" if upper is None else
                 f"> {number(lower)} a {number(upper)}")
        classes.append({"lower_exclusive": lower, "upper_inclusive": upper,
                        "color": color, "label": label})
    return {"version": VISUAL_VERSION, "classes": classes,
            "formula": config.get("formula"), "source": config.get("source", "Anexo técnico del cliente, pp. 1–2"),
            "formula_source": config.get("formula_source")}
