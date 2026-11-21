import math

from .geometry import ObjectError, parse_json


def number(value, convert=int):
    try:
        result = convert(value)
        if not math.isfinite(result):
            raise ValueError("Nonfinite style number")
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise ObjectError("invalid_style", "Style field must contain a finite number") from exc


def color(raw, default=""):
    if not raw:
        return default
    return "#" + str(raw).replace("&#035;", "").replace("#", "")


class Styles:
    def __init__(self, store):
        self.store = store
        self.settings = store.settings()
        self.count_colors = {}
        for value in self.setting("VOLS_ALLCOLOR", "valuememo").split("|"):
            if value:
                try:
                    count, raw = value.split(",", 1)
                    self.count_colors[int(count)] = color(raw)
                except ValueError as exc:
                    raise ObjectError(
                        "invalid_color_config", "Invalid VOLS_ALLCOLOR setting"
                    ) from exc

    def setting(self, name, field):
        return self.settings.get(name, {}).get(field) or ""

    def layers(self, source_kind, source_id):
        result_color = ""
        dash = 0
        for mark_id in self.store.marks(source_kind, source_id):
            layer = self.store.get("layer", mark_id)
            if not layer:
                raise ObjectError("missing_layer", f"Missing thematic layer {mark_id}")
            style = parse_json(layer.get("style"), default={})
            if style.get("COLOR"):
                result_color = color(style["COLOR"])
            if number(style.get("LINE") or 0) > 0:
                dash = number(style["LINE"])
        return result_color, {1: "20,15", 2: "1,10"}.get(dash, "")

    def node(self, row):
        node_type = self.store.get("node_type", row["node_type_id"])
        if not node_type:
            raise ObjectError("missing_node_type", "Node type is absent")
        raw = parse_json(node_type.get("style"), default={})
        planned = row.get("isplan") == 1
        template = number(raw.get("template") or 1)
        suffix = "_plan" if planned else ""
        style = {
            "shape": {1: "triangle", 2: "circle", 3: "image"}.get(template, "triangle"),
            "color": color(raw.get("color" + suffix), "#C0C0C0" if planned else "#008000"),
            "strokeColor": color(raw.get("stroke_color" + suffix), "#000000"),
            "strokeWidth": number(raw.get("stroke_width" + suffix, 1), float),
            "zIndex": number(raw.get("z") or 1),
        }
        marker_id = row.get("mapmarkcode") or 0
        marker = self.store.get("marker", marker_id) if marker_id else None
        if marker and marker.get("nazv"):
            style.update(
                shape="image", iconKey=f"customNodeIcon{marker_id}", sourceMarkerId=marker_id
            )
        elif template == 3:
            style.update(
                iconKey=f"nodeTypeIcon{'Plan' if planned else ''}{row['node_type_id']}",
                sourceIcon=raw.get("ico" + suffix, ""),
            )
        else:
            layer_color, dash = self.layers("node", row["code"])
            if dash:
                style.update(dashArray=dash, strokeWidth=5)
            if self.store.marks("node", row["code"]) and planned:
                style.update(color="#848284", strokeWidth=1)
            if layer_color:
                style.update(strokeColor=layer_color, strokeWidth=4)
        return style

    def cable(self, row):
        cable_type = self.store.get("cable_type", row["cable_line_type_id"])
        if not cable_type:
            raise ObjectError("missing_cable_type", "Cable type is absent")
        stroke = color(cable_type.get("map_color"), "#000080")
        width = number(cable_type.get("map_width") or 5, float)
        if width < 1:
            width = 5
        if self.store.children("mag", row["code"]):
            stroke = color(self.setting("VOLSM_COLOR", "valuestr"), stroke)
            value = self.setting("VOLSM_TOLS", "valuestr")
            width = number(value, float) if value else 3
        elif row.get("port") in self.count_colors:
            stroke = self.count_colors[row["port"]]
        if row.get("isplan") == 1:
            stroke = color(cable_type.get("map_color_plan"), "#848284")
        layer_color, dash = self.layers("cable", row["code"])
        stroke = layer_color or stroke
        stroke = color(row.get("colcol"), stroke)
        style = {"color": stroke, "width": int(width) if width >= 1 else 3}
        if dash:
            style["dashArray"] = dash
        return style
