"""Prototype PostGIS render input and MVT encoder; files contain raw PBF."""

from psycopg.types.json import Jsonb

from map_render.cables import (
    CABLE_SOURCE_KINDS,
    cable_type_sql,
    render_attributes,
    visible_sql,
)

from .geometry import BUFFER_PIXELS, EXTENT, MAX_LATITUDE, TILE_SIZE, WORLD, tile_bounds, tolerance

# A real empty layer, not PostGIS's zero-byte empty aggregate.
# Tile.layers(tag 3) = Layer(name='cable_line', extent=4096, version=2).
EMPTY_MVT = b"\x1a\x11\x0a\x0acable_line\x28\x80\x20\x78\x02"


def tile_inputs(conn, dataset, z, x, y, cable_type_ids=(1,), *, selection_zoom=None):
    west, south, east, north = tile_bounds(z, x, y)
    buffer = (east - west) * BUFFER_PIXELS / TILE_SIZE
    types = list(map(str, cable_type_ids))
    rows = conn.execute(
        f"""
        WITH candidates AS (
            SELECT f.*, COALESCE((SELECT array_agg(fl.layer_id ORDER BY fl.layer_id)
                FROM map_data.feature_layers fl WHERE fl.dataset_id=f.dataset_id
                AND fl.source_kind=f.source_kind AND fl.source_id=f.source_id),
                '{{}}'::bigint[]) AS layer_ids,
                ST_Simplify(ST_Transform(ST_CollectionExtract(ST_ClipByBox2D(f.geom,
                    ST_MakeEnvelope(-180,%s,180,%s,4326)::box2d),2),3857),%s,true) AS canon
            FROM map_data.features f WHERE f.dataset_id=%s
                AND f.object_type='cable_line' AND f.source_kind=ANY(%s)
                AND {visible_sql()} AND {cable_type_sql("f")}
                AND f.geom && ST_Transform(ST_MakeEnvelope(%s,%s,%s,%s,3857),4326)
        ) SELECT source_kind,source_id,object_type,min_zoom,style,properties,layer_ids,
            ST_AsGeoJSON(canon,15)::jsonb AS geometry FROM candidates
            WHERE NOT ST_IsEmpty(canon)
            AND ST_Intersects(canon,ST_MakeEnvelope(%s,%s,%s,%s,3857))
            ORDER BY source_kind,source_id
        """,
        (
            -MAX_LATITUDE,
            MAX_LATITUDE,
            tolerance(z),
            dataset,
            list(CABLE_SOURCE_KINDS),
            z if selection_zoom is None else selection_zoom,
            types,
            types,
            max(-WORLD / 2, west - buffer),
            max(-WORLD / 2, south - buffer),
            min(WORLD / 2, east + buffer),
            min(WORLD / 2, north + buffer),
            west - buffer,
            south - buffer,
            east + buffer,
            north + buffer,
        ),
    ).fetchall()
    return [{"geometry": row["geometry"], "attributes": render_attributes(row)} for row in rows]


def encode_mvt(conn, features, z, x, y):
    bounds = tile_bounds(z, x, y)
    row = conn.execute(
        """
        WITH settings AS (
            SELECT ST_MakeEnvelope(%s,%s,%s,%s,3857)::box2d AS bounds,
                %s::int AS extent, %s::int AS buffer
        ), raw AS (
            SELECT ST_SetSRID(ST_GeomFromGeoJSON(r.geometry),3857) AS geom, r.attributes
            FROM jsonb_to_recordset(%s) AS r(geometry jsonb,attributes jsonb)
        ), clipped AS (
            SELECT ST_AsMVTGeom(r.geom,s.bounds,s.extent,s.buffer,true) AS geom,
                ST_ClipByBox2D(r.geom,ST_Expand(s.bounds,
                    (ST_XMax(s.bounds)-ST_XMin(s.bounds))*s.buffer/s.extent)) AS clipped,
                r.attributes FROM raw r CROSS JOIN settings s
        ), retained AS (
            -- PostGIS can omit a tiny line whose bbox is below half an extent
            -- unit. Retain its identity as a one-unit segment at the clipped
            -- line's point: bounded quantization, still a line (round caps).
            SELECT COALESCE(c.geom,ST_AsMVTGeom(ST_MakeLine(
                ST_PointOnSurface(c.clipped), ST_Translate(ST_PointOnSurface(c.clipped),
                    (ST_XMax(s.bounds)-ST_XMin(s.bounds))/s.extent *
                    CASE WHEN ST_X(ST_PointOnSurface(c.clipped))>=ST_XMax(s.bounds)
                        THEN -1 ELSE 1 END,0)),s.bounds,s.extent,s.buffer,true)) AS geom,
                c.attributes FROM clipped c CROSS JOIN settings s
                WHERE NOT ST_IsEmpty(c.clipped)
        ), drawable AS (SELECT * FROM retained WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom))
        SELECT ST_AsMVT(drawable.*,'cable_line',%s,'geom') AS tile FROM drawable
        """,
        (*bounds, EXTENT, int(BUFFER_PIXELS * EXTENT / TILE_SIZE), Jsonb(features), EXTENT),
    ).fetchone()
    return bytes(row["tile"]) or EMPTY_MVT
