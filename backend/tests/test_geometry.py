import pytest

from map_sync.geometry import (
    GeometryBuilder,
    ObjectError,
    geometry_point,
    legacy_length,
    min_zoom,
    position,
)
from map_sync.styles import Styles


class MemoryStore:
    def __init__(self, records):
        self.records = records

    def get(self, kind, source_id):
        return self.records.get((kind, source_id))

    def children(self, kind, parent_id):
        return self.records.get((kind + "_children", parent_id), [])

    def is_plan(self, map_id):
        return map_id == 9

    def marks(self, kind, source_id):
        return self.records.get((kind + "_marks", source_id), [])

    def settings(self):
        return self.records.get("settings", {})


def builder(extra=None):
    records = {
        ("node", 1): {"geometry": {"type": "Point", "coordinates": [55, 37]}, "srid": 0},
        ("node", 2): {"geometry": {"type": "Point", "coordinates": [56, 38]}, "srid": 0},
    }
    records.update(extra or {})
    return GeometryBuilder(MemoryStore(records), "lat_lon")


def test_axes_polygon_and_building_center():
    polygon = {"type": "Polygon", "coordinates": [[[55, 37], [56, 39], [57, 38], [55, 37]]]}
    assert geometry_point(polygon, "lat_lon") == [37, 55]
    assert geometry_point(polygon, "lat_lon", building=True) == [38, 56]
    for pair in ([float("nan"), 1], [181, 55], [1], [0, 0]):
        with pytest.raises(ObjectError):
            position(pair)


@pytest.mark.parametrize("value", ["37", 37, {"0": 37}, None])
def test_coordinate_requires_array(value):
    with pytest.raises(ObjectError, match="array"):
        position(value)


@pytest.mark.parametrize("value", [42, [37, 55], {"type": "Polygon", "coordinates": {"ring": []}}])
def test_malformed_geometry_is_an_object_error(value):
    with pytest.raises(ObjectError):
        geometry_point(value, "lat_lon")


@pytest.mark.parametrize("field,value", [("z", "bad"), ("template", [1]), ("stroke_width", "NaN")])
def test_invalid_node_style_is_an_object_error(field, value):
    styles = Styles(MemoryStore({("node_type", 4): {"style": {field: value}}}))
    with pytest.raises(ObjectError) as error:
        styles.node({"code": 1, "node_type_id": 4})
    assert error.value.code == "invalid_style"


def test_inheritance_missing_parent_and_cycle():
    assert builder({("node", 3): {"parentuzel": 1}}).node(3) == [37, 55]
    for records, code in [
        ({("node", 3): {"parentuzel": 4}, ("node", 4): {"parentuzel": 3}}, "parent_cycle"),
        ({("node", 3): {"parentuzel": 100}}, "missing_node"),
        ({("node", 3): {}}, "missing_coordinate"),
    ]:
        with pytest.raises(ObjectError) as caught:
            builder(records).node(3)
        assert caught.value.code == code


def test_places_priority_and_plan_exclusion():
    geom = builder(
        {
            ("place_children", 10): [
                {"mapcode": 9, "x1": 1, "y1": 1},
                {"mapcode": 3, "x1": 37.5, "y1": 55.5},
            ],
            ("route_object_children", 10): [{"obj2": 2, "usercode2": 100}],
        }
    )
    coords, source = geom.cable({"code": 10, "uzelcode1": 1, "uzelcode2": 2})
    assert coords == [[37, 55], [37.5, 55.5], [38, 56]]
    assert source == "places"


def test_intermediate_objects_override_ducts():
    geom = builder(
        {
            ("route_object_children", 10): [
                {"obj2": 2, "usercode2": 1},
                {"obj2": 5, "additional_data_string": '{"coordinates":"55.5,37.5"}'},
            ],
            ("duct_content_children", 10): [{"cable_duct_id": 100}],
        }
    )
    coords, source = geom.cable({"code": 10, "uzelcode1": 1, "uzelcode2": 2})
    assert coords == [[37, 55], [37.5, 55.5], [38, 56]]
    assert source == "objects"


def test_duct_reverse_and_building_finish():
    geom = builder(
        {
            ("duct_content_children", 10): [{"cable_duct_id": 5, "direction": 1}],
            ("duct", 5): {"cable_route_id": 6},
            ("route", 6): {"coord": '["55.8,37.8","55.2,37.2"]'},
            ("building", 7): {"geometry": {"type": "Point", "coordinates": [56, 38]}},
        }
    )
    coords, source = geom.cable({"code": 10, "uzelcode1": 1, "building2_id": 7})
    assert coords == [[37, 55], [37.2, 55.2], [37.8, 55.8], [38, 56]]
    assert source == "ducts"


def test_route_endpoint_fallback_and_invalid_line():
    geom = builder(
        {
            ("route", 6): {
                "object_first_type": 17,
                "object_first_id": 1,
                "object_second_type": 22,
                "object_second_id": 2,
            }
        }
    )
    assert geom.route(6) == [[37, 55], [38, 56]]
    with pytest.raises(ObjectError, match="distinct"):
        geom.cable({"code": 10, "uzelcode1": 1, "uzelcode2": 1})


@pytest.mark.parametrize(
    "length,zoom", [(0, 14), (49, 14), (50, 13), (100, 12), (250, 11), (500, 10), (1000, 1)]
)
def test_zoom_boundaries(length, zoom):
    assert min_zoom(length) == zoom


def test_length_matches_legacy_segment_rounding():
    assert legacy_length([[37, 55], [37, 55.001], [37, 55.002]]) == 224


def test_style_override_order():
    store = MemoryStore(
        {
            ("cable_type", 1): {"map_color": "00ff00", "map_color_plan": "aaaaaa", "map_width": 5},
            ("cable_marks", 10): [3],
            ("layer", 3): {"style": '{"COLOR":"ff0000","LINE":2}'},
            ("mag_children", 10): [{"volscode": 10}],
            "settings": {"VOLSM_COLOR": {"valuestr": "0000ff"}, "VOLSM_TOLS": {"valuestr": "7"}},
        }
    )
    style = Styles(store).cable(
        {"code": 10, "cable_line_type_id": 1, "isplan": 1, "colcol": "123456"}
    )
    assert style == {"color": "#123456", "width": 7, "dashArray": "1,10"}
