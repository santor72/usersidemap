import json
from pathlib import Path

import pytest

from map_render.cables import encode_layer_ids, min_zoom, render_attributes, visible_cable

FIXTURE = json.loads((Path(__file__).parent / "fixtures/cable_tiles.json").read_text())


@pytest.mark.parametrize(
    "zoom,layers,key",
    [
        (13, None, "z13"),
        (14, None, "z14"),
        (14, [1], "z14layer1"),
        (14, [11], "z14layer11"),
    ],
)
def test_input_ids_before_spatial_clipping(zoom, layers, key):
    selected = [
        render_attributes(f)["objectId"]
        for f in FIXTURE["features"]
        if visible_cable(f, zoom, [1], layers)
    ]
    assert selected == FIXTURE["expectedNonSpatial"][key]


@pytest.mark.parametrize(
    "length,zoom",
    [
        (0.1, 14),
        (49.999, 14),
        (50, 13),
        (99.999, 13),
        (100, 12),
        (250, 11),
        (500, 10),
        (999.999, 10),
        (1000, 1),
    ],
)
def test_legacy_zoom_thresholds(length, zoom):
    assert min_zoom(length) == zoom


def test_group_is_not_clipped_by_type_or_thematic_filter():
    group = FIXTURE["features"][4]
    assert visible_cable(group, 14, [2], [11])
    assert render_attributes(group)["objectId"] == "cable_group:5"
    assert len(group["geometry"]["coordinates"]) == 2
    assert render_attributes(group)["layerIds"] == ",1,11,"


def test_scalar_style_preserves_object_renderer_values():
    solid, dashed, group = (FIXTURE["features"][i] for i in (1, 2, 4))
    for feature in (solid, dashed, group):
        attrs = render_attributes(feature)
        assert attrs["__mapColor"] == feature["style"]["color"]
        assert attrs["__mapWidth"] == feature["style"]["width"]
        assert attrs["__mapDashArray"] == feature["style"].get("dashArray", "")
        assert all(isinstance(v, (str, int, float)) for v in attrs.values())
    assert render_attributes(solid)["renderOrder"] == 1
    assert render_attributes(dashed)["renderOrder"] == 0


def test_layer_id_encoding_does_not_match_substrings():
    assert encode_layer_ids([11, 1, 11]) == ",1,11,"
    assert ",1," not in encode_layer_ids([11])
    assert encode_layer_ids([]) == ","
    with pytest.raises(ValueError):
        encode_layer_ids([0])
