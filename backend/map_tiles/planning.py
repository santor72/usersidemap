"""Sparse XYZ plan: intersect each cable part with buffered tile envelopes."""

from map_render.cables import CABLE_SOURCE_KINDS, cable_type_sql, visible_sql

from .geometry import BUFFER_PIXELS, MAX_LATITUDE, TILE_SIZE, WORLD, tolerance


def selection_zoom(format, xyz_zoom):
    if format not in ("vector", "raster"):
        raise ValueError("Unknown tile format")
    # MapLibre vector sources have 512 logical px, raster sources here 256.
    return max(0, xyz_zoom - (format == "raster"))


def plan_zoom(conn, dataset, z, *, format="vector", cable_type_ids=(1,)):
    if type(z) is not int or not 0 <= z <= 24:
        raise ValueError("Invalid tile zoom")
    count = 2**z
    span = WORLD / count
    buffer = span * BUFFER_PIXELS / TILE_SIZE
    types = list(map(str, cable_type_ids))
    rows = conn.execute(
        f"""
        WITH pieces AS MATERIALIZED (
            SELECT (ST_Dump(ST_Simplify(ST_Transform(ST_CollectionExtract(ST_ClipByBox2D(f.geom,
                ST_MakeEnvelope(-180,%s,180,%s,4326)::box2d),2),3857),%s,true))).geom AS geom
            FROM map_data.features f WHERE f.dataset_id=%s AND f.object_type='cable_line'
            AND f.source_kind=ANY(%s) AND {visible_sql()} AND {cable_type_sql("f")}
        ), candidates AS (
            SELECT p.geom, x, y FROM pieces p
            CROSS JOIN LATERAL generate_series(
                greatest(0,floor((ST_XMin(Box3D(p.geom))-%s+%s)/%s)::int),
                least(%s,floor((ST_XMax(Box3D(p.geom))+%s+%s)/%s)::int)) x
            CROSS JOIN LATERAL generate_series(
                greatest(0,floor((%s-ST_YMax(Box3D(p.geom))-%s)/%s)::int),
                least(%s,floor((%s-ST_YMin(Box3D(p.geom))+%s)/%s)::int)) y
            WHERE NOT ST_IsEmpty(p.geom)
        ) SELECT DISTINCT x,y FROM candidates
            WHERE ST_Intersects(geom,ST_Expand(ST_TileEnvelope(%s,x,y),%s))
            ORDER BY x,y
        """,
        (
            -MAX_LATITUDE,
            MAX_LATITUDE,
            tolerance(z),
            dataset,
            list(CABLE_SOURCE_KINDS),
            selection_zoom(format, z),
            types,
            types,
            buffer,
            WORLD / 2,
            span,
            count - 1,
            buffer,
            WORLD / 2,
            span,
            WORLD / 2,
            buffer,
            span,
            count - 1,
            WORLD / 2,
            buffer,
            span,
            z,
            buffer,
        ),
    ).fetchall()
    return [(row["x"], row["y"]) for row in rows]
