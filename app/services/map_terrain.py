"""Open Mapzen Terrarium elevation, rendered as ordinary PNG tiles (no WebGL)."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
import numpy as np
from PIL import Image

from app.core.config import settings

TERRAIN_SOURCE = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRAIN_CREDIT = "Mapzen Terrain Tiles · fuentes SRTM/GMTED, © sus proveedores · https://github.com/tilezen/joerd/blob/master/docs/attribution.md"
_locks: dict[str, asyncio.Lock] = {}


def cache_dir() -> Path:
    folder = settings.storage_path / "regional-map"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
        temporary.write(data)
        name = Path(temporary.name)
    try:
        name.replace(path)
    finally:
        name.unlink(missing_ok=True)


async def cached_download(url: str, relative: str, *, max_bytes: int = 20_000_000) -> bytes:
    path = cache_dir() / relative
    if path.is_file():
        return path.read_bytes()
    lock = _locks.setdefault(relative, asyncio.Lock())
    async with lock:
        if path.is_file():
            return path.read_bytes()
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            async with client.stream(
                "GET", url, headers={"User-Agent": "FECOCLIMA-IA/1.0 open-map"}
            ) as response:
                response.raise_for_status()
                chunks = bytearray()
                async for part in response.aiter_bytes():
                    chunks.extend(part)
                    if len(chunks) > max_bytes:
                        raise ValueError("La fuente cartográfica excedió el tamaño permitido")
        atomic_write(path, bytes(chunks))
        return bytes(chunks)


def decode_terrarium(data: bytes) -> np.ndarray:
    with Image.open(BytesIO(data)) as image:
        if image.size != (256, 256):
            raise ValueError("Tesela de elevación inválida")
        rgb = np.asarray(image.convert("RGB"), dtype=float)
    return rgb[:, :, 0] * 256 + rgb[:, :, 1] + rgb[:, :, 2] / 256 - 32768


async def elevation(z: int, x: int, y: int) -> np.ndarray:
    return decode_terrarium(
        await cached_download(
            TERRAIN_SOURCE.format(z=z, x=x, y=y), f"dem/{z}/{x}/{y}.png", max_bytes=1_000_000
        )
    )


def tile_coordinates(z: int, x: int, y: int, size: int = 256):
    n = 2**z
    longitude = (x + (np.arange(size) + 0.5) / size) / n * 360 - 180
    mercator = np.pi * (1 - 2 * (y + (np.arange(size) + 0.5) / size) / n)
    latitude = np.degrees(np.arctan(np.sinh(mercator)))
    return np.meshgrid(longitude, latitude)


def png(array: np.ndarray) -> bytes:
    buffer = BytesIO()
    Image.fromarray(array.astype(np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def colorize(values: np.ndarray, minimum: float, maximum: float, *, mask=None, palette=None) -> np.ndarray:
    palette = palette or ["#2d668f", "#67b6b2", "#bfdb91", "#f3cd70", "#bd654e"]
    colors = np.array([[int(color[i : i + 2], 16) for i in (1, 3, 5)] for color in palette])
    ratio = np.clip((values - minimum) / max(1e-9, maximum - minimum), 0, 1) * (len(colors) - 1)
    indices = np.minimum(ratio.astype(int), len(colors) - 2)
    fraction = (ratio - indices)[..., None]
    rgb = colors[indices] * (1 - fraction) + colors[indices + 1] * fraction
    alpha = np.full(values.shape, 220, dtype=np.uint8)
    if mask is not None:
        alpha[~mask] = 0
    return np.dstack([rgb.astype(np.uint8), alpha])


def terrain_image(heights: np.ndarray, kind: str, z: int, y: int) -> bytes:
    _, latitudes = tile_coordinates(z, 0, y)
    meters = 40075016.686 / (2**z * 256) * max(0.01, np.cos(np.radians(float(latitudes[128, 128]))))
    dy, dx = np.gradient(heights, meters)
    slope = np.degrees(np.arctan(np.hypot(dx, dy)))
    if kind == "altitude":
        image = colorize(heights, 0, 1500, palette=["#4b8655", "#d8d496", "#ad8859", "#f4f1e6"])
    elif kind == "slope":
        image = colorize(slope, 0, 45, palette=["#e6f0c5", "#efcb67", "#dc8258", "#a54558"])
    elif kind == "aspect":
        aspect = (np.degrees(np.arctan2(-dx, dy)) + 360) % 360
        image = colorize(
            aspect, 0, 360, mask=slope >= 2, palette=["#4771a8", "#73b9a5", "#e0c878", "#c97d83", "#4771a8"]
        )
    else:
        normal = np.sqrt(dx * dx + dy * dy + 1)
        shade = np.clip((-dx * (-0.5) - dy * (-0.5) + 0.707) / normal, 0, 1)
        gray = (90 + shade * 165).astype(np.uint8)
        image = np.dstack([gray, gray, gray, np.full(gray.shape, 170, dtype=np.uint8)])
    return png(image)


async def terrain_tile(kind: str, z: int, x: int, y: int) -> bytes:
    path = cache_dir() / f"terrain-v1/{kind}/{z}/{x}/{y}.png"
    if path.is_file():
        return path.read_bytes()
    result = terrain_image(await elevation(z, x, y), kind, z, y)
    atomic_write(path, result)
    return result
