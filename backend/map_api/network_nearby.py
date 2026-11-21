"""Partner lookup against one published Userside map."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from map_render.cables import cable_type_sql

from .tile_auth import NETWORK_NEARBY_SCOPE, load_token_access, reject

_PARTNER_BEARER = HTTPBearer(scheme_name="NetworkPartnerToken", auto_error=False)
_RESULT_KEYS = {"cable_line": "cables", "node": "nodes", "coupler": "couplers"}
_POINT_TYPES = ("node", "coupler")
_MAIN_LIMIT = 3
_ALTERNATIVE_LIMIT = 2
_LIMIT = f"CASE WHEN types.kind = 'cable_line' THEN {_MAIN_LIMIT} ELSE %(point_limit)s END"
_DISTANCE = "ST_Distance(f.geom::geography, point.geog)"
_CABLE_TYPE_FILTER = cable_type_sql("f").replace("%s", "%(cable_types)s")
# ST_Distance rounds at sub-micrometre precision. Pad the index search and
# apply the same reported distance to decide whether a boundary point is in.
_INDEX_PADDING_M = 0.000001
_LOOKUP = """
    WITH point AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s),4326)::geography AS geog)
    SELECT types.kind, nearest.source_kind, nearest.source_id, nearest.distance_m
    FROM point CROSS JOIN unnest(%(kinds)s::text[]) AS types(kind)
"""
_ELIGIBLE = f"""
    FROM map_data.features f
    WHERE f.dataset_id = %(version)s AND f.object_type = types.kind
      AND NOT f.hidden AND NOT f.planned
      AND (types.kind <> 'cable_line' OR {_CABLE_TYPE_FILTER})
      AND (types.kind = 'cable_line' OR f.source_id <> ALL(%(excluded_points)s::bigint[]))
"""
_RANKED = f"""
    CROSS JOIN LATERAL (
        SELECT f.source_kind, f.source_id, {_DISTANCE} AS distance_m
        {_ELIGIBLE}
        AND ST_DWithin(f.geom::geography, point.geog, {{radius}})
        {{distance_filter}}
        ORDER BY distance_m, f.source_kind, f.source_id
        LIMIT {_LIMIT}
    ) AS nearest
    ORDER BY types.kind, nearest.distance_m, nearest.source_kind, nearest.source_id
"""
WITHIN_RADIUS_SQL = _LOOKUP + _RANKED.format(
    radius=f"%(radius)s + {_INDEX_PADDING_M}",
    distance_filter=f"AND {_DISTANCE} <= %(radius)s",
)

# Geography KNN orders by spherical distance. The seeds give an upper bound
# on the last requested spheroidal distance. Search every object up to that bound and
# rank exactly, including ties. The padding covers floating-point rounding.
NEAREST_SQL = (
    _LOOKUP
    + f"""
    CROSS JOIN LATERAL (
        SELECT max(seed.distance_m) + {_INDEX_PADDING_M} AS radius
        FROM (
            SELECT {_DISTANCE} AS distance_m
            {_ELIGIBLE}
            ORDER BY f.geom::geography <-> point.geog
            LIMIT {_LIMIT}
        ) AS seed
        HAVING count(*) > 0
    ) AS bound
"""
    + _RANKED.format(radius="bound.radius", distance_filter="")
)

CONNECTED_CABLES_SQL = f"""
    WITH cables AS NOT MATERIALIZED (
        SELECT f.source_kind, f.source_id, f.properties
        FROM map_data.features f
        WHERE f.dataset_id = %(version)s AND f.object_type = 'cable_line'
          AND NOT f.hidden AND NOT f.planned AND {_CABLE_TYPE_FILTER}
    )
    SELECT DISTINCT point_id, cable_id, group_id
    FROM (
        SELECT link.target_id AS point_id, cable.source_id AS cable_id, NULL::bigint AS group_id
        FROM map_data.feature_links link
        JOIN cables cable USING (source_kind, source_id)
        WHERE link.dataset_id = %(version)s AND link.source_kind = 'cable'
          AND link.target_kind = 'node' AND link.role IN ('start', 'end')
          AND link.target_id = ANY(%(point_ids)s::bigint[])
        UNION ALL
        SELECT (endpoint.ref->>'sourceId')::bigint AS point_id,
               (member.item->>'sourceId')::bigint AS cable_id, cable.source_id AS group_id
        FROM cables cable
        CROSS JOIN LATERAL jsonb_array_elements(cable.properties->'members') AS member(item)
        CROSS JOIN LATERAL (VALUES (member.item->'start'), (member.item->'end')) AS endpoint(ref)
        WHERE cable.source_kind = 'cable_group'
          AND member.item->>'status' IN ('original', 'reconstructed')
          AND member.item->>'cableTypeId' = ANY(%(cable_types)s)
          AND NOT COALESCE((member.item->>'hidden')::boolean, false)
          AND NOT COALESCE((member.item->>'planned')::boolean, false)
          AND endpoint.ref->>'sourceKind' = 'node'
          AND (endpoint.ref->>'sourceId')::bigint = ANY(%(point_ids)s::bigint[])
    ) AS connected
    ORDER BY point_id, cable_id, group_id
"""


class NearbyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(strict=True, ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(strict=True, ge=-180, le=180, allow_inf_nan=False)
    radiusM: float = Field(
        strict=True,
        ge=0,
        allow_inf_nan=False,
        description="Search radius in metres. Zero requests the nearest objects across the map.",
    )


class NearbyObject(BaseModel):
    objectId: str
    distanceM: float


class ConnectedCable(BaseModel):
    cableId: str
    groupId: str | None = None


class NearbyPoint(NearbyObject):
    connectedCables: list[ConnectedCable]


class AlternativePoint(NearbyPoint):
    withinRadius: bool


class NearbyAlternatives(BaseModel):
    nodes: list[AlternativePoint]
    couplers: list[AlternativePoint]


class NearbyResponse(BaseModel):
    mapId: int
    datasetVersion: UUID
    radiusM: float
    searchMode: Literal["within_radius", "nearest_outside_radius", "nearest", "empty"]
    cables: list[NearbyObject]
    nodes: list[NearbyPoint]
    couplers: list[NearbyPoint]
    alternatives: NearbyAlternatives


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def install_network_nearby(app, source_system, cable_type_ids, set_timeouts):
    @app.post(
        "/api/network/nearby",
        summary="Find network infrastructure near a point",
        response_model_exclude_none=True,
        responses={
            401: {"model": ErrorResponse, "description": "Missing or invalid partner token"},
            403: {"model": ErrorResponse, "description": "Token lacks access to network lookup"},
            422: {"model": ErrorResponse, "description": "Invalid coordinates or radius"},
            503: {"model": ErrorResponse, "description": "Published network data is unavailable"},
        },
    )
    async def nearby(
        body: NearbyRequest,
        request: Request,
        response: Response,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_PARTNER_BEARER)],
    ) -> NearbyResponse:
        if credentials is None:
            reject(401, "AUTH_REQUIRED", "Bearer token required")

        async with request.app.state.pool.connection() as conn, conn.transaction():
            await set_timeouts(conn)
            access = await load_token_access(conn, credentials.credentials)
            if NETWORK_NEARBY_SCOPE not in access["scopes"] or len(access["map_ids"]) != 1:
                reject(403, "ACCESS_DENIED", "Token requires network:nearby access to one map")
            (map_id,) = access["map_ids"]
            pinned = await (
                await conn.execute(
                    "SELECT map_data.api_pin_active_dataset(%s,%s) AS version",
                    (source_system, map_id),
                )
            ).fetchone()
            version = pinned["version"]
            if version is None:
                reject(503, "NO_ACTIVE_DATASET", "No published network dataset for this map")

            params = {
                "lat": body.latitude,
                "lon": body.longitude,
                "radius": body.radiusM,
                "version": version,
                "kinds": list(_RESULT_KEYS),
                "cable_types": cable_type_ids,
                "point_limit": _MAIN_LIMIT,
                "excluded_points": [],
            }
            nearest_only = body.radiusM == 0
            rows = []
            if not nearest_only:
                rows = await (await conn.execute(WITHIN_RADIUS_SQL, params)).fetchall()
            if rows:
                mode = "within_radius"
                main_point_ids = [row["source_id"] for row in rows if row["kind"] in _POINT_TYPES]
                extra_params = params | {
                    "kinds": list(_POINT_TYPES),
                    "point_limit": _ALTERNATIVE_LIMIT,
                    "excluded_points": main_point_ids,
                }
                alternatives = await (await conn.execute(NEAREST_SQL, extra_params)).fetchall()
            else:
                ranked_rows = await (
                    await conn.execute(
                        NEAREST_SQL, params | {"point_limit": _MAIN_LIMIT + _ALTERNATIVE_LIMIT}
                    )
                ).fetchall()
                rows, alternatives = [], []
                counts = dict.fromkeys(_POINT_TYPES, 0)
                for row in ranked_rows:
                    kind = row["kind"]
                    if kind not in counts:
                        rows.append(row)
                        continue
                    counts[kind] += 1
                    (rows if counts[kind] <= _MAIN_LIMIT else alternatives).append(row)
                mode = "nearest" if nearest_only else "nearest_outside_radius"
                if not rows:
                    mode = "empty"

            point_ids = [
                row["source_id"] for row in rows + alternatives if row["kind"] in _POINT_TYPES
            ]
            connections = {point_id: [] for point_id in point_ids}
            if point_ids:
                links = await (
                    await conn.execute(CONNECTED_CABLES_SQL, params | {"point_ids": point_ids})
                ).fetchall()
                for link in links:
                    group_id = link["group_id"]
                    connections[link["point_id"]].append(
                        ConnectedCable(
                            cableId=f"cable:{link['cable_id']}",
                            groupId=None if group_id is None else f"cable_group:{group_id}",
                        )
                    )

        groups = {key: [] for key in _RESULT_KEYS.values()}
        extra_groups = {_RESULT_KEYS[kind]: [] for kind in _POINT_TYPES}
        for is_extra, result_rows in ((False, rows), (True, alternatives)):
            for row in result_rows:
                kind = row["kind"]
                key = _RESULT_KEYS[kind]
                fields = {
                    "objectId": f"{row['source_kind']}:{row['source_id']}",
                    "distanceM": row["distance_m"],
                }
                if kind == "cable_line":
                    groups[key].append(NearbyObject(**fields))
                    continue
                fields["connectedCables"] = connections[row["source_id"]]
                if is_extra:
                    extra_groups[key].append(
                        AlternativePoint(**fields, withinRadius=row["distance_m"] <= body.radiusM)
                    )
                else:
                    groups[key].append(NearbyPoint(**fields))
        response.headers["Cache-Control"] = "no-store"
        return NearbyResponse(
            mapId=map_id,
            datasetVersion=version,
            radiusM=body.radiusM,
            searchMode=mode,
            alternatives=NearbyAlternatives(**extra_groups),
            **groups,
        )
