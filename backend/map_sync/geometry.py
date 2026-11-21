import json
import math
from itertools import pairwise

from map_render.cables import min_zoom  # noqa: F401 -- compatibility export


class ObjectError(Exception):
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def parse_json(value, *, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ObjectError("invalid_json", "Invalid JSON in map source data") from exc
    if isinstance(default, dict) and not isinstance(value, dict):
        raise ObjectError("invalid_json", "Expected a JSON object in map source data")
    return value


def position(pair, axes="lon_lat"):
    if not isinstance(pair, (list, tuple)):
        raise ObjectError("invalid_coordinate", "Coordinate must be a numeric array")
    try:
        a, b = pair[:2]
        lon, lat = (float(b), float(a)) if axes == "lat_lon" else (float(a), float(b))
    except (TypeError, ValueError, IndexError) as exc:
        raise ObjectError("invalid_coordinate", "Coordinate must contain two numbers") from exc
    if (
        not math.isfinite(lon)
        or not math.isfinite(lat)
        or not (-180 <= lon <= 180 and -90 <= lat <= 90)
    ):
        raise ObjectError("invalid_coordinate", "Coordinate outside longitude/latitude bounds")
    if lon == 0 and lat == 0:
        raise ObjectError("missing_coordinate", "Zero coordinate sentinel")
    return [round(lon, 6), round(lat, 6)]


def geometry_point(geometry, axes, *, building=False):
    if not geometry:
        raise ObjectError("missing_coordinate", "No source geometry")
    if not isinstance(geometry, dict):
        raise ObjectError("unsupported_geometry", "Source geometry must be an object")
    coords = geometry.get("coordinates")
    if geometry.get("type") == "Point":
        return position(coords, axes)
    if (
        geometry.get("type") == "Polygon"
        and isinstance(coords, list)
        and coords
        and isinstance(coords[0], list)
        and coords[0]
    ):
        if building:
            # Legacy uses the envelope center, not the polygon centroid.
            points = [position(pair, axes) for pair in coords[0]]
            return [
                round((min(p[0] for p in points) + max(p[0] for p in points)) / 2, 6),
                round((min(p[1] for p in points) + max(p[1] for p in points)) / 2, 6),
            ]
        return position(coords[0][0], axes)
    raise ObjectError("unsupported_geometry", "Expected Point or Polygon source geometry")


def legacy_length(coords):
    total = 0
    for a, b in pairwise(coords):
        lon1, lat1, lon2, lat2 = map(math.radians, [a[0], a[1], b[0], b[1]])
        delta = lon2 - lon1
        y = math.hypot(
            math.cos(lat2) * math.sin(delta),
            math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta),
        )
        x = math.sin(lat1) * math.sin(lat2) + math.cos(lat1) * math.cos(lat2) * math.cos(delta)
        total += math.ceil(math.atan2(y, x) * 6372795)
    return total


class GeometryBuilder:
    def __init__(self, store, axes):
        self.store = store
        self.axes = axes

    def node(self, node_id):
        visited = set()
        current = node_id
        while current:
            if current in visited:
                raise ObjectError("parent_cycle", "Cycle in node coordinate inheritance")
            visited.add(current)
            row = self.store.get("node", current)
            if row is None:
                raise ObjectError("missing_node", f"Missing node dependency {current}")
            if row.get("srid") not in (None, 0, 4326):
                raise ObjectError("unsupported_srid", "Source geometry SRID requires reprojection")
            if row.get("geometry"):
                return geometry_point(row["geometry"], self.axes)
            current = row.get("parentuzel")
        raise ObjectError("missing_coordinate", "Node and its parents have no coordinates")

    def building(self, building_id):
        row = self.store.get("building", building_id)
        if not row:
            raise ObjectError("missing_building", f"Missing building dependency {building_id}")
        if row.get("srid") not in (None, 0, 4326):
            raise ObjectError("unsupported_srid", "Building SRID requires reprojection")
        return geometry_point(row.get("geometry"), self.axes, building=True)

    def route_endpoint(self, row, side):
        object_type = row.get(f"object_{side}_type")
        object_id = row.get(f"object_{side}_id")
        if object_type in (17, 18, 22):
            return self.node(object_id)
        if object_type == 19:
            return self.building(row.get(f"{side}_building_id"))
        if object_type == 1:
            customer = self.store.get("customer", object_id)
            if not customer:
                raise ObjectError("missing_customer", "Route customer dependency is absent")
            # Legacy resolves customer building_id but then uses the route building field.
            return self.building(row.get(f"{side}_building_id") or customer.get("building_id"))
        raise ObjectError("unsupported_endpoint", "Unknown cable route endpoint type")

    def route(self, route_id):
        row = self.store.get("route", route_id)
        if not row:
            raise ObjectError("missing_route", f"Missing route dependency {route_id}")
        if row.get("coord"):
            values = parse_json(row["coord"])
            if not isinstance(values, list) or not values:
                raise ObjectError("invalid_route", "Route coordinates must be a nonempty array")
            return [position(v.split(",") if isinstance(v, str) else v, "lat_lon") for v in values]
        return [self.route_endpoint(row, side) for side in ("first", "second")]

    def cable(self, row):
        coords = [self.node(row.get("uzelcode1"))]
        places = [
            r
            for r in self.store.children("place", row["code"])
            if not self.store.is_plan(r.get("mapcode"))
        ]
        objects = self.store.children("route_object", row["code"]) if not places else []
        route_source = "direct"
        if places:
            route_source = "places"
            coords.extend(position([r["x1"], r["y1"]]) for r in places)
        elif objects:
            route_source = "objects"
            for obj in objects:
                if obj["obj2"] == 2:
                    coords.append(self.node(obj["usercode2"]))
                else:
                    extra = parse_json(obj.get("additional_data_string"), default={})
                    if extra.get("coordinates"):
                        if not isinstance(extra["coordinates"], str):
                            raise ObjectError(
                                "invalid_coordinate", "Intermediate coordinates must be a string"
                            )
                        coords.append(position(extra["coordinates"].split(","), "lat_lon"))
        else:
            for content in self.store.children("duct_content", row["code"]):
                route_source = "ducts"
                duct = self.store.get("duct", content["cable_duct_id"])
                if not duct:
                    raise ObjectError("missing_duct", "Missing duct dependency")
                segment = self.route(duct.get("cable_route_id"))
                coords.extend(reversed(segment) if (content.get("direction") or 0) > 0 else segment)
        end = (
            self.building(row["building2_id"])
            if (row.get("building2_id") or 0) > 0
            else self.node(row.get("uzelcode2"))
        )
        coords.append(end)
        # Repeated join vertices do not change a route; remove consecutive duplicates only.
        coords = [p for i, p in enumerate(coords) if i == 0 or p != coords[i - 1]]
        if len(coords) < 2:
            raise ObjectError("degenerate_line", "Cable has no distinct coordinates")
        return coords, route_source
