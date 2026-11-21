"""Cairo strokes -> straight RGBA -> separately encoded lossless WebP."""

import io
import sys

import cairocffi as cairo
from PIL import Image, ImageColor

from .geometry import TILE_SIZE, tile_bounds

WEBP_OPTIONS = {"lossless": True, "quality": 80, "method": 4, "exact": True}


def surface_rgba(surface):
    surface.flush()
    width, height, stride = surface.get_width(), surface.get_height(), surface.get_stride()
    # Pillow's native raw decoder and RGBa conversion handle unpremultiplication
    # without a Python loop over every pixel (especially costly for @2x).
    raw_mode = "BGRa" if sys.byteorder == "little" else "aRGB"
    with Image.frombytes(
        "RGBa", (width, height), bytes(surface.get_data()), "raw", raw_mode, stride, 1
    ) as image:
        return image.convert("RGBA")


def render_webp(features, z, x, y, density=1):
    if density not in (1, 2):
        raise ValueError("Raster density must be 1 or 2")
    west, _south, east, north = tile_bounds(z, x, y)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, TILE_SIZE * density, TILE_SIZE * density)
    context = cairo.Context(surface)
    context.scale(density, density)
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    context.set_line_join(cairo.LINE_JOIN_ROUND)
    scale = TILE_SIZE / (east - west)
    for feature in sorted(
        features, key=lambda f: (f["attributes"]["renderOrder"], f["attributes"]["objectId"])
    ):
        attributes, geometry = feature["attributes"], feature["geometry"]
        color = ImageColor.getrgb(attributes["__mapColor"])
        context.set_source_rgb(*(channel / 255 for channel in color[:3]))
        context.set_line_width(attributes["__mapWidth"])
        # Dash is intentionally ignored, including former-dashed groups.
        lines = (
            geometry["coordinates"]
            if geometry["type"] == "MultiLineString"
            else [geometry["coordinates"]]
        )
        for line in lines:
            if len(line) < 2:
                continue
            context.move_to((line[0][0] - west) * scale, (north - line[0][1]) * scale)
            for longitude, latitude in line[1:]:
                context.line_to((longitude - west) * scale, (north - latitude) * scale)
            context.stroke()
    image = surface_rgba(surface)
    del context
    surface.finish()
    with image, io.BytesIO() as result:
        image.save(result, "WEBP", **WEBP_OPTIONS)
        return result.getvalue()
