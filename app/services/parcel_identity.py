"""Stable parcel identity and per-browser-session creation quota."""
from hashlib import sha256
from shapely import set_precision
from shapely.geometry import shape


def geometry_key(geometry: dict) -> str:
    # Ignore ring orientation/start vertex and sub-centimetre coordinate noise.
    normalized = set_precision(shape(geometry), 1e-7).normalize()
    return sha256(normalized.wkb).hexdigest()


def producer_label(name: str | None, document: str | None) -> str:
    return f"{name} · CI {document}" if name and document else name or 'Sin productor'
