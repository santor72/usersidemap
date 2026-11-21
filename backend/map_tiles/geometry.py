"""Shared geometry canon for MVT and Cairo, in Web Mercator metres."""

import hashlib
import json
import math

WORLD = 40075016.68557849
EXTENT = 4096
TILE_SIZE = 256
BUFFER_PIXELS = 8
MAX_LATITUDE = 85.0511287798066


def tile_bounds(z, x, y):
    if not (
        all(type(value) is int for value in (z, x, y))
        and 0 <= z <= 24
        and 0 <= x < 2**z
        and 0 <= y < 2**z
    ):
        raise ValueError("Invalid XYZ coordinates")
    span = WORLD / 2**z
    west = -WORLD / 2 + x * span
    north = WORLD / 2 - y * span
    return west, north - span, west + span, north


def tolerance(z):
    # Preserve full native short-cable geometry at z14 and above.
    return WORLD / (TILE_SIZE * 2**z) * 0.5 if z < 14 else 0.0


def canon(maxzoom):
    value = {
        "algorithm": "ST_Simplify(geom3857,tolerance,preserveCollapsed=true)",
        "projection": "ST_Transform(ST_ClipByBox2D(geom4326,mercatorBounds),3857)",
        "tolerances": {str(z): tolerance(z) for z in range(maxzoom + 1)},
        "extent": EXTENT,
        "tileSize": TILE_SIZE,
        "bufferPixels": BUFFER_PIXELS,
        "mvtCollapsedLine": "one-extent-unit-segment-at-clipped-point-on-surface",
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return value, hashlib.sha256(encoded).hexdigest()


def xyz(longitude, latitude, z):
    latitude = max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude))
    size = 2**z
    x = math.floor((longitude + 180) / 360 * size)
    y = math.floor((1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * size)
    return max(0, min(size - 1, x)), max(0, min(size - 1, y))
