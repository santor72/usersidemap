"""Rules operate on published snapshots, never on live Userside records.

Spatial selection is supplied by the caller: API uses an unbuffered WGS84 bbox;
tiles use a buffered XYZ envelope. A group is selected as one logical object.
"""

RULES_VERSION = "cables-v1"
CABLE_SOURCE_KINDS = ("cable", "cable_group")
DASH_PATTERNS = ("20,15", "1,10")


def min_zoom(length):
    for threshold, zoom in ((50, 14), (100, 13), (250, 12), (500, 11), (1000, 10)):
        if length < threshold:
            return zoom
    return 1


def cable_type_sql(alias=""):
    """SQL uses two array parameters, preserving scalar and group JSON semantics."""
    if alias not in ("", "f"):
        raise ValueError("Unsupported SQL alias")
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}properties->>'cableTypeId'=ANY(%s) OR {prefix}properties->'cableTypeIds' ?| %s)"
    )


def visible_sql(alias="f"):
    if alias not in ("", "f"):
        raise ValueError("Unsupported SQL alias")
    prefix = f"{alias}." if alias else ""
    return f"{prefix}hidden=false AND {prefix}min_zoom<=%s"


def layer_filter_sql():
    return (
        "EXISTS (SELECT 1 FROM map_data.feature_layers fl "
        "WHERE fl.dataset_id=f.dataset_id AND fl.source_kind=f.source_kind "
        "AND fl.source_id=f.source_id AND fl.layer_id=ANY(%s))"
    )


def visible_cable(feature, zoom, cable_type_ids, layer_ids=None):
    """Reference predicate for fixtures and renderer input parity (before bbox)."""
    if (
        feature["object_type"] != "cable_line"
        or feature["source_kind"] not in CABLE_SOURCE_KINDS
        or feature["hidden"]
        or feature["min_zoom"] > zoom
    ):
        return False
    properties = feature["properties"]
    types = set(properties.get("cableTypeIds", []))
    if "cableTypeId" in properties:
        types.add(str(properties["cableTypeId"]))
    if not types.intersection(map(str, cable_type_ids)):
        return False
    return not layer_ids or bool(set(layer_ids).intersection(feature.get("layer_ids", [])))


def encode_layer_ids(layer_ids):
    values = sorted(set(layer_ids))
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("Layer IDs must be positive integers")
    return "," + ",".join(map(str, values)) + ("," if values else "")


def render_attributes(feature):
    """Scalar MVT contract compatible with prepareRenderFeature/cable-style.ts.

    Keep snapshot style values unchanged. Only absent/null fields use the same
    fallbacks as the MapLibre object layer. Cairo ignores __mapDashArray.
    """
    kind, source_id = feature["source_kind"], feature["source_id"]
    if kind not in CABLE_SOURCE_KINDS:
        raise ValueError("Unsupported cable source kind")
    style = feature["style"]
    backbone = bool(feature["properties"].get("isBackbone", False))
    return {
        "objectId": f"{kind}:{source_id}",
        "sourceKind": kind,
        "sourceId": source_id,
        "min_zoom": feature["min_zoom"],
        "layerIds": encode_layer_ids(feature.get("layer_ids", [])),
        "__mapColor": style["color"] if style.get("color") is not None else "#0877d1",
        "__mapWidth": style["width"] if style.get("width") is not None else 3,
        "__mapDashArray": style.get("dashArray") or "",
        "renderOrder": int(backbone),
    }
