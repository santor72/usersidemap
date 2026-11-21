import io

import pytest
from PIL import Image

from map_tiles.geometry import TILE_SIZE, canon, tile_bounds, xyz


def raster_module():
    try:
        from map_tiles import raster
    except OSError:
        pytest.skip("Cairo native library required; run in tile worker image")
    return raster


def pixel_line(z=14, x=9876, y=5182, color="#ff0000", width=3, dash="20,15", order=0):
    west, _south, east, north = tile_bounds(z, x, y)
    unit = (east - west) / TILE_SIZE
    return {
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [west - 20 * unit, north - 128.3 * unit],
                [east + 20 * unit, north - 128.3 * unit],
            ],
        },
        "attributes": {
            "objectId": "cable:1",
            "__mapColor": color,
            "__mapWidth": width,
            "__mapDashArray": dash,
            "renderOrder": order,
        },
    }


def test_transparency_straight_alpha_solid_and_retina_width():
    raster = raster_module()
    feature = pixel_line()
    for density in (1, 2):
        image = Image.open(io.BytesIO(raster.render_webp([feature], 14, 9876, 5182, density)))
        assert image.size == (256 * density, 256 * density)
        image = image.convert("RGBA")
        assert image.getpixel((0, 0)) == (0, 0, 0, 0)
        # Former dashed route is solid, including both tile edges.
        assert all(image.getpixel((x, 128 * density))[3] == 255 for x in range(image.width))
        antialias = [p for p in image.get_flattened_data() if 0 < p[3] < 255]
        assert antialias
        assert all(p[:3] == (255, 0, 0) for p in antialias)
        alpha_width = sum(image.getpixel((128 * density, y))[3] / 255 for y in range(image.height))
        assert alpha_width / density == pytest.approx(3, abs=0.02)


def test_backbone_is_above_other_cables_regardless_of_input_order():
    raster = raster_module()
    top = pixel_line(color="#0000ff", order=1)
    bottom = pixel_line(color="#ff0000", order=0)
    for inputs in ([top, bottom], [bottom, top]):
        image = Image.open(io.BytesIO(raster.render_webp(inputs, 14, 9876, 5182)))
        assert image.convert("RGBA").getpixel((128, 128)) == (0, 0, 255, 255)


def test_empty_raster_and_geometry_canon():
    raster = raster_module()
    for density in (1, 2):
        image = Image.open(io.BytesIO(raster.render_webp([], 14, 9876, 5182, density)))
        assert image.convert("RGBA").getbbox() is None
    table, digest = canon(14)
    assert table["tolerances"]["14"] == 0
    assert digest == canon(14)[1]
    assert xyz(180, 90, 14) == (16383, 0)


@pytest.mark.parametrize(
    "endianness,pixel",
    [
        ("little", bytes((0, 0, 128, 128))),
        ("big", bytes((128, 128, 0, 0))),
    ],
)
def test_cairo_alpha_conversion_respects_stride_and_native_endian(monkeypatch, endianness, pixel):
    raster = raster_module()

    class Surface:
        def flush(self):
            pass

        def get_width(self):
            return 1

        def get_height(self):
            return 2

        def get_stride(self):
            return 8

        def get_data(self):
            return pixel + b"\xff" * 4 + bytes(8)

    monkeypatch.setattr(raster.sys, "byteorder", endianness)
    with raster.surface_rgba(Surface()) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0, 128)
        assert image.getpixel((0, 1)) == (0, 0, 0, 0)
