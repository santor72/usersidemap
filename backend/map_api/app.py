import json
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from map_render.cables import cable_type_sql, layer_filter_sql, visible_sql

from .batching import BatchPacker
from .cable_details import cable_endpoint_names
from .network_nearby import install_network_nearby
from .providers import PROVIDERS
from .tile_auth import authorize
from .tile_routes import install_tile_routes

_LOCAL_SETTINGS = {
    key: value
    for key, value in dotenv_values(Path.cwd() / ".env").items()
    if key == "MAP_DB_URL" or key.startswith("MAP_API_")
}


def env_setting(key, default):
    return os.getenv(key) or _LOCAL_SETTINGS.get(key) or default


class TilePin(BaseModel):
    tileVersion: UUID
    format: Literal["vector", "raster"]


class PreloadRequest(BaseModel):
    mapId: int = Field(gt=0)
    bbox: list[float]
    zoom: int = Field(ge=0, le=24)
    pointMinZoom: int | None = Field(default=None, ge=0, le=22)
    objectTypes: list[str]
    layerIds: list[int] | None = None
    pin: TilePin | None = None

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value):
        if len(value) != 4 or not all(math.isfinite(x) for x in value):
            raise ValueError("bbox must contain four finite coordinates")
        west, south, east, north = value
        if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox coordinates are outside geographic bounds")
        return value

    @field_validator("objectTypes")
    @classmethod
    def valid_types(cls, value):
        if any(item not in PROVIDERS for item in value):
            raise ValueError("objectTypes contains an unsupported type")
        return list(dict.fromkeys(value))

    @field_validator("layerIds")
    @classmethod
    def valid_layer_ids(cls, value):
        if value is None:
            return value
        if any(layer_id <= 0 for layer_id in value):
            raise ValueError("layerIds must contain positive IDs")
        return list(dict.fromkeys(value))


class BatchRequest(BaseModel):
    queryId: UUID
    batchIndex: int = Field(ge=0)


def error(status, code, message):
    raise HTTPException(status, detail={"code": code, "message": message})


def configured_cable_types(settings, key, env_key):
    cable_type_ids = settings.get(key, env_setting(env_key, "1"))
    if isinstance(cable_type_ids, str):
        cable_type_ids = [
            int(value.strip()) for value in cable_type_ids.split(",") if value.strip()
        ]
    cable_type_ids = [int(value) for value in cable_type_ids]
    if any(value <= 0 for value in cable_type_ids):
        raise ValueError(f"{env_key} must contain positive IDs")
    return [str(value) for value in dict.fromkeys(cable_type_ids)]


def create_app(pool=None, settings=None):
    settings = settings or {}
    cable_type_ids = configured_cable_types(
        settings, "visible_cable_type_ids", "MAP_API_VISIBLE_CABLE_TYPE_IDS"
    )
    network_cable_type_ids = configured_cable_types(
        settings, "network_cable_type_ids", "MAP_API_NETWORK_CABLE_TYPE_IDS"
    )
    dsn = (
        settings.get("database_url")
        or env_setting("MAP_API_DATABASE_URL", "")
        or env_setting("MAP_DB_URL", "")
    )
    own_pool = pool is None

    @asynccontextmanager
    async def lifespan(app):
        if own_pool:
            if not dsn:
                raise RuntimeError("Set MAP_API_DATABASE_URL")
            app.state.pool = AsyncConnectionPool(
                dsn,
                min_size=int(settings.get("pool_min", env_setting("MAP_API_POOL_MIN", "1"))),
                max_size=int(settings.get("pool_max", env_setting("MAP_API_POOL_MAX", "10"))),
                timeout=float(
                    settings.get("pool_timeout", env_setting("MAP_API_POOL_TIMEOUT", "5"))
                ),
                open=False,
                kwargs={
                    "row_factory": dict_row,
                    "autocommit": False,
                    "connect_timeout": int(
                        settings.get(
                            "connect_timeout_seconds",
                            env_setting("MAP_API_CONNECT_TIMEOUT_SECONDS", "10"),
                        )
                    ),
                },
            )
            await app.state.pool.open()
            try:
                yield
            finally:
                await app.state.tile_status_cache.close()
                await app.state.pool.close()
        else:
            app.state.pool = pool
            try:
                yield
            finally:
                await app.state.tile_status_cache.close()

    app = FastAPI(title="Userside Map API", version="1.0.0", lifespan=lifespan)
    origins = settings.get(
        "cors_origins",
        env_setting("MAP_API_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"),
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[x.strip() for x in origins if x.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization", "If-None-Match"],
        expose_headers=["ETag", "Content-Encoding"],
        allow_credentials="*" not in [origin.strip() for origin in origins],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request, exc):
        detail = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"code": "REQUEST_ERROR", "message": str(exc.detail)}
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": detail},
            headers={"Cache-Control": "no-store", **(exc.headers or {})},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc):
        return JSONResponse(
            status_code=422,
            headers={"Cache-Control": "no-store"},
            content={
                "error": {"code": "INVALID_REQUEST", "message": "Request parameters are invalid"}
            },
        )

    @app.exception_handler(psycopg.Error)
    async def database_error_handler(request, exc):
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={
                "error": {"code": "DATABASE_UNAVAILABLE", "message": "Map database is unavailable"}
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request, exc):
        return JSONResponse(
            status_code=500,
            headers={"Cache-Control": "no-store"},
            content={
                "error": {"code": "INTERNAL_ERROR", "message": "The request could not be completed"}
            },
        )

    async def set_timeouts(conn):
        await conn.execute(
            "SELECT set_config('statement_timeout',%s,true)",
            (
                str(
                    settings.get(
                        "statement_timeout_ms", env_setting("MAP_API_STATEMENT_TIMEOUT_MS", "30000")
                    )
                ),
            ),
        )
        await conn.execute(
            "SELECT set_config('lock_timeout',%s,true)",
            (str(settings.get("lock_timeout_ms", env_setting("MAP_API_LOCK_TIMEOUT_MS", "5000"))),),
        )

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    from .tile_ops import install_tile_ops

    install_tile_ops(app, settings, set_timeouts)

    @app.get("/health/ready")
    async def ready(request: Request):
        try:
            async with request.app.state.pool.connection() as conn:
                await set_timeouts(conn)
                row = await (
                    await conn.execute("""SELECT PostGIS_Version() AS postgis,
                  to_regclass('map_data.features') IS NOT NULL AS schema_ok,
                  to_regclass('map_data.active_datasets') IS NOT NULL AS active_ok,
                  to_regprocedure('map_data.api_pin_active_dataset(text,bigint)') IS NOT NULL AS pin_ok,
                  to_regclass('map_data.tile_versions') IS NOT NULL AS tiles_ok,
                  to_regclass('map_data.api_tokens') IS NOT NULL AS tokens_ok,
                  to_regprocedure('map_data.api_pin_tile_dataset(text,bigint,text,uuid)') IS NOT NULL AS tile_pin_ok""")
                ).fetchone()
            if not all(
                row[key]
                for key in (
                    "schema_ok",
                    "active_ok",
                    "pin_ok",
                    "tiles_ok",
                    "tokens_ok",
                    "tile_pin_ok",
                )
            ):
                error(503, "SCHEMA_UNAVAILABLE", "Required map schema is unavailable")
            return {"status": "ready", "postgis": row["postgis"]}
        except HTTPException:
            raise
        except (OSError, RuntimeError, ValueError, psycopg.Error):
            error(503, "DATABASE_UNAVAILABLE", "Map database is unavailable")

    @app.get("/api/map/layers")
    async def layers(mapId: int, request: Request):
        async with request.app.state.pool.connection() as conn:
            await set_timeouts(conn)
            rows = await (
                await conn.execute(
                    """SELECT d.id AS dataset_id FROM map_data.active_datasets a
              JOIN map_data.datasets d ON d.id=a.dataset_id
              WHERE a.source_system=%s AND a.map_id=%s AND d.status='ready'""",
                    (
                        settings.get(
                            "source_system", env_setting("MAP_API_SOURCE_SYSTEM", "userside")
                        ),
                        mapId,
                    ),
                )
            ).fetchone()
            if not rows:
                error(503, "DATASET_UNAVAILABLE", "No published map version is available")
            dataset_id = rows["dataset_id"]
            types = await (
                await conn.execute(
                    f"""SELECT object_type, count(*)::int AS count,
              min(min_zoom)::int AS min_zoom FROM map_data.features WHERE dataset_id=%s AND hidden=false
              AND (object_type<>'cable_line' OR {cable_type_sql()})
              GROUP BY object_type ORDER BY object_type""",
                    (dataset_id, cable_type_ids, cable_type_ids),
                )
            ).fetchall()
            layer_rows = await (
                await conn.execute(
                    """SELECT l.source_id, l.title, l.default_visible, l.style,
              count(fl.source_id)::int AS object_count FROM map_data.layers l LEFT JOIN map_data.feature_layers fl
              ON fl.dataset_id=l.dataset_id AND fl.layer_id=l.source_id WHERE l.dataset_id=%s
              GROUP BY l.dataset_id,l.source_id ORDER BY l.source_id""",
                    (dataset_id,),
                )
            ).fetchall()
            version = await (
                await conn.execute(
                    "SELECT published_at FROM map_data.datasets WHERE id=%s", (dataset_id,)
                )
            ).fetchone()
            return {
                "datasetVersion": str(dataset_id),
                "publishedAt": version["published_at"],
                "objectTypes": types,
                "layers": layer_rows,
            }

    @app.post("/api/map/objects/preload")
    async def preload(body: PreloadRequest, request: Request):
        query_id = uuid4()
        max_objects = int(settings.get("max_objects", env_setting("MAP_API_MAX_OBJECTS", "100000")))
        max_vertices = int(
            settings.get("max_vertices", env_setting("MAP_API_MAX_VERTICES", "2000000"))
        )
        hard_vertices = int(
            settings.get("hard_vertices", env_setting("MAP_API_HARD_VERTICES", "100000"))
        )
        batch_objects = int(
            settings.get("batch_objects", env_setting("MAP_API_BATCH_OBJECTS", "100"))
        )
        batch_vertices = int(
            settings.get("batch_vertices", env_setting("MAP_API_BATCH_VERTICES", "5000"))
        )
        ttl = int(settings.get("ttl_seconds", env_setting("MAP_API_TTL_SECONDS", "600")))
        preload_timeout = float(
            settings.get(
                "preload_timeout_seconds", env_setting("MAP_API_PRELOAD_TIMEOUT_SECONDS", "120")
            )
        )
        deadline = time.monotonic() + preload_timeout
        west, south, east, north = body.bbox
        async with request.app.state.pool.connection() as conn:
            async with conn.transaction():
                await set_timeouts(conn)
                if body.pin:
                    await authorize(conn, request, body.mapId, "metadata")
                    config = request.app.state.tile_config
                    version = await (
                        await conn.execute(
                            "SELECT map_id,format,source_system,status FROM map_data.tile_versions WHERE id=%s",
                            (body.pin.tileVersion,),
                        )
                    ).fetchone()
                    if not version or (
                        version["map_id"],
                        version["format"],
                        version["source_system"],
                    ) != (body.mapId, body.pin.format, config.source):
                        error(404, "TILE_VERSION_NOT_FOUND", "Pinned release not found")
                    if version["status"] in ("retired", "deleted"):
                        error(410, "TILE_VERSION_RETIRED", "Pinned release was retired")
                    active = await (
                        await conn.execute(
                            "SELECT map_data.api_pin_tile_dataset(%s,%s,%s,%s) AS dataset_id",
                            (config.source, body.mapId, body.pin.format, body.pin.tileVersion),
                        )
                    ).fetchone()
                    body.objectTypes = [kind for kind in body.objectTypes if kind != "cable_line"]
                    if not active or not active["dataset_id"]:
                        error(410, "TILE_VERSION_RETIRED", "Pinned release is unavailable")
                    dataset_id = active["dataset_id"]
                else:
                    active = await (
                        await conn.execute(
                            """SELECT map_data.api_pin_active_dataset(%s,%s) AS dataset_id""",
                            (
                                settings.get(
                                    "source_system",
                                    env_setting("MAP_API_SOURCE_SYSTEM", "userside"),
                                ),
                                body.mapId,
                            ),
                        )
                    ).fetchone()
                    if not active:
                        error(503, "DATASET_UNAVAILABLE", "No published map version is available")
                    dataset_id = active["dataset_id"]
                await conn.execute(
                    """INSERT INTO map_data.query_leases(id,dataset_id,expires_at,query,batches)
                      VALUES (%s,%s,clock_timestamp(),%s,'[]')""",
                    (
                        query_id,
                        dataset_id,
                        json.dumps(
                            {
                                "mapId": body.mapId,
                                "bbox": body.bbox,
                                "zoom": body.zoom,
                                "pointMinZoom": body.pointMinZoom,
                                "objectTypes": body.objectTypes,
                                "layerIds": body.layerIds,
                                "pin": body.pin.model_dump(mode="json") if body.pin else None,
                            }
                        ),
                    ),
                )
                summary = []
                type_counts = {}
                total = 0
                vertices = 0
                packer = BatchPacker(batch_objects, batch_vertices)

                async def store_batch(packed):
                    batch_index = len(summary)
                    descriptor = {
                        "index": batch_index,
                        "objectCount": packed.object_count,
                        "vertexCount": packed.vertex_count,
                        "oversized": packed.oversized,
                    }
                    await conn.execute(
                        "INSERT INTO map_data.query_batches VALUES (%s,%s,%s,%s,%s)",
                        (
                            query_id,
                            batch_index,
                            packed.object_count,
                            packed.vertex_count,
                            packed.oversized,
                        ),
                    )
                    async with conn.cursor() as item_cursor:
                        await item_cursor.executemany(
                            "INSERT INTO map_data.query_items VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            [
                                (
                                    query_id,
                                    batch_index,
                                    order,
                                    dataset_id,
                                    row["source_kind"],
                                    row["source_id"],
                                    row["object_type"],
                                    row["vertex_count"],
                                )
                                for order, row in enumerate(packed.items)
                            ],
                        )
                    summary.append(descriptor)

                if body.objectTypes:
                    bbox_expr = "ST_MakeEnvelope(%s,%s,%s,%s,4326)"
                    if west <= east:
                        spatial = f"ST_Intersects(f.geom,{bbox_expr})"
                        spatial_params = [west, south, east, north]
                    else:
                        spatial = f"(ST_Intersects(f.geom,{bbox_expr}) OR ST_Intersects(f.geom,{bbox_expr}))"
                        spatial_params = [west, south, 180, north, -180, south, east, north]
                    layer_sql = "" if not body.layerIds else "AND " + layer_filter_sql()
                    type_clauses = []
                    params = [dataset_id]
                    for object_type in body.objectTypes:
                        provider = PROVIDERS[object_type]
                        clause = "(f.object_type=%s AND f.source_kind=ANY(%s)"
                        params.extend([provider.object_type, list(provider.source_kinds)])
                        if object_type == "cable_line":
                            clause += " AND " + cable_type_sql("f")
                            params.extend([cable_type_ids, cable_type_ids])
                        type_clauses.append(clause + ")")
                    visibility = visible_sql()
                    if body.pointMinZoom is None:
                        params.append(body.zoom)
                    else:
                        visibility = """f.hidden=false AND CASE
                            WHEN f.object_type IN ('node','coupler') THEN %s<=%s
                            ELSE f.min_zoom<=%s END"""
                        params.extend([body.pointMinZoom, body.zoom, body.zoom])
                    params.extend(spatial_params)
                    if body.layerIds:
                        params.append(body.layerIds)
                    sql = f"""SELECT f.source_kind,f.source_id,f.object_type,f.vertex_count FROM map_data.features f
                          WHERE f.dataset_id=%s AND ({" OR ".join(type_clauses)})
                          AND {visibility}
                          AND {spatial} {layer_sql} ORDER BY f.source_kind,f.source_id"""
                    async with conn.cursor(name="preload_metadata") as cur:
                        await cur.execute(sql, params)
                        async for row in cur:
                            if time.monotonic() >= deadline:
                                error(
                                    503,
                                    "PRELOAD_TIMEOUT",
                                    "Selection preparation exceeded its time limit",
                                )
                            n = row["vertex_count"]
                            total += 1
                            vertices += n
                            if n > hard_vertices:
                                error(
                                    413,
                                    "OBJECT_TOO_LARGE",
                                    "A selected object exceeds the vertex limit",
                                )
                            if total > max_objects or vertices > max_vertices:
                                error(
                                    413,
                                    "QUERY_LIMIT_EXCEEDED",
                                    "Selection exceeds configured limits",
                                )
                            type_counts[row["object_type"]] = (
                                type_counts.get(row["object_type"], 0) + 1
                            )
                            for packed in packer.push(row):
                                await store_batch(packed)
                last_batch = packer.finish()
                if last_batch:
                    await store_batch(last_batch)
                await conn.execute(
                    "INSERT INTO map_data.query_lease_totals VALUES (%s,%s,%s,%s)",
                    (query_id, total, vertices, json.dumps(type_counts)),
                )
                await conn.execute(
                    "UPDATE map_data.query_leases SET expires_at=clock_timestamp()+(%s * interval '1 second'),batches=%s WHERE id=%s",
                    (ttl, json.dumps(summary), query_id),
                )
                expires = await (
                    await conn.execute(
                        "SELECT expires_at FROM map_data.query_leases WHERE id=%s", (query_id,)
                    )
                ).fetchone()
            return {
                "queryId": str(query_id),
                "datasetVersion": str(dataset_id),
                "expiresAt": expires["expires_at"],
                "totalObjects": total,
                "totalVertices": vertices,
                "typeCounts": type_counts,
                "batches": summary,
            }

    async def lease(conn, query_id):
        row = await (
            await conn.execute(
                """SELECT id,dataset_id,expires_at,query FROM map_data.query_leases
          WHERE id=%s FOR SHARE""",
                (query_id,),
            )
        ).fetchone()
        if not row:
            error(404, "QUERY_NOT_FOUND", "Query was not found")
        exp = await (
            await conn.execute("SELECT clock_timestamp() >= %s AS expired", (row["expires_at"],))
        ).fetchone()
        if exp["expired"]:
            error(410, "QUERY_EXPIRED", "Query has expired; create a new preload")
        return row

    @app.post("/api/map/objects/batch")
    async def batch(body: BatchRequest, request: Request):
        query_id = body.queryId
        batch_index = body.batchIndex
        async with request.app.state.pool.connection() as conn, conn.transaction():
            await set_timeouts(conn)
            q = await lease(conn, query_id)
            if q["query"].get("pin"):
                await authorize(conn, request, q["query"]["mapId"], "metadata")
            b = await (
                await conn.execute(
                    "SELECT object_count FROM map_data.query_batches WHERE query_id=%s AND batch_index=%s",
                    (query_id, batch_index),
                )
            ).fetchone()
            if not b:
                error(404, "BATCH_NOT_FOUND", "Batch was not found")
            rows = await (
                await conn.execute(
                    """SELECT f.source_kind,f.source_id,f.object_type,ST_AsGeoJSON(f.geom)::jsonb AS geom,f.style,f.title
                  FROM map_data.query_items i JOIN map_data.features f USING(dataset_id,source_kind,source_id)
                  WHERE i.query_id=%s AND i.batch_index=%s ORDER BY i.item_order""",
                    (query_id, batch_index),
                )
            ).fetchall()
            feats = []
            for r in rows:
                feats.append(
                    {
                        "type": "Feature",
                        "id": f"{r['source_kind']}:{r['source_id']}",
                        "geometry": r["geom"],
                        "properties": {
                            "sourceKind": r["source_kind"],
                            "sourceId": r["source_id"],
                            "objectType": r["object_type"],
                            "title": r["title"],
                            "style": r["style"],
                        },
                    }
                )
            return {
                "queryId": str(query_id),
                "datasetVersion": str(q["dataset_id"]),
                "batchIndex": batch_index,
                "objectCount": len(feats),
                "featureCollection": {"type": "FeatureCollection", "features": feats},
            }

    @app.get("/api/map/objects/{source_kind}/{source_id}")
    async def details(source_kind: str, source_id: int, queryId: UUID, request: Request):
        async with request.app.state.pool.connection() as conn, conn.transaction():
            await set_timeouts(conn)
            q = await lease(conn, queryId)
            if q["query"].get("pin"):
                await authorize(conn, request, q["query"]["mapId"], "details")
            row = await (
                await conn.execute(
                    """SELECT f.title,f.properties,f.length_m,f.object_type,
                  COALESCE((SELECT jsonb_agg(jsonb_build_object('role',l.role,'targetKind',l.target_kind,'targetId',l.target_id) ORDER BY l.role) FROM map_data.feature_links l WHERE l.dataset_id=f.dataset_id AND l.source_kind=f.source_kind AND l.source_id=f.source_id),'[]') AS links,
                  COALESCE((SELECT jsonb_agg(jsonb_build_object('id',ly.source_id,'title',ly.title) ORDER BY ly.source_id) FROM map_data.feature_layers fl JOIN map_data.layers ly ON ly.dataset_id=fl.dataset_id AND ly.source_id=fl.layer_id WHERE fl.dataset_id=f.dataset_id AND fl.source_kind=f.source_kind AND fl.source_id=f.source_id),'[]') AS layers
                  FROM map_data.query_items i JOIN map_data.features f USING(dataset_id,source_kind,source_id)
                  WHERE i.query_id=%s AND i.source_kind=%s AND i.source_id=%s""",
                    (queryId, source_kind, source_id),
                )
            ).fetchone()
            if not row:
                error(404, "OBJECT_NOT_FOUND", "Object is not part of this query")
            if row["object_type"] == "cable_line":
                row["cableEndpoints"] = await cable_endpoint_names(conn, q["dataset_id"], row)
            return {
                "queryId": str(queryId),
                "datasetVersion": str(q["dataset_id"]),
                "sourceKind": source_kind,
                "sourceId": source_id,
                **row,
            }

    install_tile_routes(app, settings, set_timeouts)
    install_network_nearby(
        app,
        settings.get("source_system", env_setting("MAP_API_SOURCE_SYSTEM", "userside")),
        network_cable_type_ids,
        set_timeouts,
    )
    return app


app = create_app()
