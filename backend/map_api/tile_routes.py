"""Retained metadata, immutable XYZ and snapshot details for external GIS."""

import gzip
import hashlib
import json
import re
import threading
import time
from functools import lru_cache
from typing import Literal
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from map_render.cables import CABLE_SOURCE_KINDS, cable_type_sql, render_attributes
from map_tiles.geometry import tile_bounds
from map_tiles.lifecycle import TileError
from map_tiles.publication import safe_path
from map_tiles.status_cache import AsyncVersionStatusCache
from map_tiles.vector import EMPTY_MVT

from .cable_details import cable_endpoint_names
from .providers import PROVIDERS
from .tile_auth import authorize, reject
from .tile_settings import TileSettings
from .tile_signing import templates, verify_signature

META_SELECT = """SELECT v.id,v.map_id,v.format,v.source_system,v.dataset_version,v.status,
    v.minzoom,v.maxzoom,v.display_maxzoom,v.final_path,v.rules_version,v.rules_hash,
    v.manifest->'bounds' AS bounds,v.manifest->'cableTypeIds' AS cable_types,
    d.published_at AS dataset_published_at
    FROM map_data.tile_versions v LEFT JOIN map_data.datasets d ON d.id=v.dataset_id"""


def retained(row, config, map_id, format=None):
    if (
        not row
        or row["map_id"] != map_id
        or row["source_system"] != config.source
        or (format and row["format"] != format)
    ):
        reject(404, "TILE_VERSION_NOT_FOUND", "Tile version not found")
    if row["status"] in ("retired", "deleted"):
        reject(410, "TILE_VERSION_RETIRED", "Tile version was retired; refresh catalog")
    if row["status"] != "ready":
        reject(404, "TILE_VERSION_NOT_FOUND", "Tile version is not ready")


def private_json(request, body):
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, "Vary": "Authorization, Cookie"}
    if etag in [tag.strip() for tag in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/json", headers=headers)


def descriptor(config, row, active_dataset, now):
    template, template2x, expiry = templates(config, row, now)
    vector = row["format"] == "vector"
    base = f"{config.public_base}/api/maps/{row['map_id']}/tiles/{row['format']}/{row['id']}"
    return {
        "tileVersion": str(row["id"]),
        "datasetVersion": str(row["dataset_version"]),
        "minzoom": row["minzoom"],
        "maxzoom": row["maxzoom"],
        "displayMaxzoom": row["display_maxzoom"],
        "tileSize": 512 if vector else 256,
        "template": template,
        "template2x": template2x,
        "templateExpiresAt": expiry,
        "tilejsonUrl": base + "/tilejson.json",
        "styleUrl": base + "/style.json" if vector else None,
        "prebakeMaxzoom": None if vector else row["maxzoom"],
        "datasetAge": max(0, int(now - row["dataset_published_at"].timestamp()))
        if row["dataset_published_at"]
        else 0,
        "tilesBehindActive": bool(active_dataset and row["dataset_version"] != active_dataset),
        "capabilities": {"layerFilter": vector, "hitTest": vector, "dash": vector},
    }


def vector_style(row, item):
    width = ["coalesce", ["get", "__mapWidth"], 3]

    def dash(on, off):
        return [
            "case",
            *[
                part
                for n in range(1, 6)
                for part in (["<=", width, n], ["literal", [on / n, off / n]])
            ],
            ["literal", [on / 6, off / 6]],
        ]

    dashed = ["in", ["get", "__mapDashArray"], ["literal", ["20,15", "1,10"]]]
    paint = {
        "line-color": ["coalesce", ["get", "__mapColor"], "#0877d1"],
        "line-width": width,
        "line-opacity": 1,
    }
    layout = {"line-cap": "round", "line-join": "round", "line-sort-key": ["get", "renderOrder"]}
    common = {"type": "line", "source": "cables", "source-layer": "cable_line", "layout": layout}
    layers = [
        {
            **common,
            "id": "cable_line-tiles",
            "filter": ["all", ["!", dashed], ["<=", ["get", "min_zoom"], ["zoom"]]],
            "paint": paint,
        },
        {
            **common,
            "id": "cable_line-dashed-tiles",
            "filter": ["all", dashed, ["<=", ["get", "min_zoom"], ["zoom"]]],
            "paint": {
                **paint,
                "line-dasharray": [
                    "case",
                    ["==", ["get", "__mapDashArray"], "20,15"],
                    dash(20, 15),
                    dash(1, 10),
                ],
            },
        },
    ]
    return {
        "version": 8,
        "sources": {
            "cables": {
                "type": "vector",
                "tiles": [item["template"]],
                "minzoom": row["minzoom"],
                "maxzoom": row["maxzoom"],
                "promoteId": "objectId",
            }
        },
        "layers": layers,
        "metadata": {
            "tileVersion": str(row["id"]),
            "datasetVersion": str(row["dataset_version"]),
            "displayMaxzoom": row["display_maxzoom"],
            "templateExpiresAt": item["templateExpiresAt"],
        },
    }


_INDEX_LOCK = threading.Lock()


@lru_cache(maxsize=4)
def file_index(root, final_path):
    folder = safe_path(root, final_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    files = {}
    for z, entries in manifest["coverage"]["native"].items():
        for entry in entries:
            if entry["status"] != "prebaked":
                raise ValueError("Native coverage is incomplete")
            files[(int(z), entry["x"], entry["y"])] = entry
    return manifest, files


# Preencoded transparent WebP: API requires no Cairo/Pillow runtime.
EMPTY_WEBP = {
    1: bytes.fromhex(
        "5249464622000000574542505650384c150000002fffc03f10071011110050a4ffff29a2ffa9fffd0700"
    ),
    2: bytes.fromhex(
        "524946462c000000574542505650384c1f0000002fffc17f1007101111100824fb7bcf5044ff33fef39ffffce73ffff9cfff0100"
    ),
}


def empty_webp(density):
    return EMPTY_WEBP[density]


def tile_payload(config, row, z, x, y, density):
    # Single-flight initialization: a large raster manifest must not be fetched
    # and decoded once for every concurrent first tile request.
    with _INDEX_LOCK:
        manifest, index = file_index(config.root, row["final_path"])
    if (
        manifest["tileVersion"] != row["id"]
        or manifest["mapId"] != row["map_id"]
        or manifest["format"] != row["format"]
    ):
        raise ValueError("Manifest identity mismatch")
    if z > manifest["maxzoom"]:
        reject(422, "TILE_OVERZOOM", "Use client overzoom above native maxzoom")
    if z < manifest["minzoom"]:
        reject(422, "INVALID_TILE_COORDINATES", "Zoom is outside native coverage")
    entry = index.get((z, x, y))
    if entry is None:
        return EMPTY_MVT if row["format"] == "vector" else empty_webp(density)
    if row["format"] == "vector":
        record = entry
        relative = f"{z}/{x}/{y}.pbf"
    else:
        record = entry["files"][str(density)]
        relative = f"{z}/{x}/{y}{'@2x' if density == 2 else ''}.webp"
    if record["path"] != relative:
        raise ValueError("Unsafe manifest file path")
    payload = safe_path(config.root, row["final_path"] + "/" + relative).read_bytes()
    if len(payload) != record["bytes"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise ValueError("Tile checksum mismatch")
    return payload


def install_tile_routes(app, settings, set_timeouts):
    config = TileSettings.load(settings)
    cache = AsyncVersionStatusCache.from_url(config.redis_url, ttl=config.status_ttl)
    app.state.tile_status_cache = cache
    app.state.tile_config = config
    now = settings.get("tiles_clock", time.time)

    async def fetch(sql, params):
        async with app.state.pool.connection() as conn:
            await set_timeouts(conn)
            return await (await conn.execute(sql, params)).fetchone()

    async def metadata_row(conn, map_id, version, format=None):
        row = await (await conn.execute(META_SELECT + " WHERE v.id=%s", (version,))).fetchone()
        retained(row, config, map_id, format)
        return row

    @app.get("/api/maps/{map_id}/tiles/catalog")
    async def catalog(map_id: int, request: Request):
        async with app.state.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await set_timeouts(conn)
            await authorize(conn, request, map_id, "metadata")
            active = await (
                await conn.execute(
                    "SELECT dataset_id FROM map_data.active_datasets WHERE source_system=%s AND map_id=%s",
                    (config.source, map_id),
                )
            ).fetchone()
            dataset = active["dataset_id"] if active else None
            pointers = await (
                await conn.execute(
                    "SELECT * FROM map_data.active_tile_versions WHERE source_system=%s AND map_id=%s",
                    (config.source, map_id),
                )
            ).fetchall()
            body = {
                "mapId": map_id,
                "tiles_lag": config.lag_policy,
                "catalogPollSeconds": config.catalog_poll_seconds,
                "activeDatasetVersion": str(dataset) if dataset else None,
                "layers": [],
                "formats": {f: {"active": None, "prev": None} for f in ("vector", "raster")},
            }
            timestamp = now()
            layer_datasets = {dataset} if dataset else set()
            for pointer in pointers:
                for slot, key in (("active", "active_version"), ("prev", "prev_version")):
                    if pointer[key]:
                        row = await metadata_row(conn, map_id, pointer[key], pointer["format"])
                        body["formats"][pointer["format"]][slot] = descriptor(
                            config, row, dataset, timestamp
                        )
                        layer_datasets.add(row["dataset_version"])
            if layer_datasets:
                layers = await (
                    await conn.execute(
                        """SELECT DISTINCT ON(source_id) source_id AS id,title,default_visible AS "defaultVisible"
                    FROM map_data.layers WHERE dataset_id=ANY(%s) ORDER BY source_id,(dataset_id=%s) DESC""",
                        (list(layer_datasets), dataset),
                    )
                ).fetchall()
                body["layers"] = layers
        return private_json(request, body)

    @app.get("/api/maps/{map_id}/tiles/{format}/{version}/tilejson.json")
    async def tilejson(
        map_id: int, format: Literal["vector", "raster"], version: UUID, request: Request
    ):
        async with app.state.pool.connection() as conn:
            await set_timeouts(conn)
            await authorize(conn, request, map_id, "metadata")
            row = await metadata_row(conn, map_id, version, format)
        item = descriptor(config, row, None, now())
        body = {
            "tilejson": "3.0.0",
            "scheme": "xyz",
            "tiles": [item["template"]],
            "bounds": row["bounds"] or [-180, -85.05112878, 180, 85.05112878],
            "minzoom": row["minzoom"],
            "maxzoom": row["maxzoom"],
            "tileSize": item["tileSize"],
            "tileVersion": str(version),
            "datasetVersion": str(row["dataset_version"]),
            "displayMaxzoom": row["display_maxzoom"],
            "templateExpiresAt": item["templateExpiresAt"],
        }
        if format == "raster":
            body.update(prebakeMaxzoom=row["maxzoom"], template2x=item["template2x"])
        else:
            body["vector_layers"] = [
                {
                    "id": "cable_line",
                    "fields": {
                        k: v
                        for k, v in (
                            ("objectId", "String"),
                            ("sourceKind", "String"),
                            ("sourceId", "Number"),
                            ("layerIds", "String"),
                            ("min_zoom", "Number"),
                            ("__mapColor", "String"),
                            ("__mapWidth", "Number"),
                            ("__mapDashArray", "String"),
                            ("renderOrder", "Number"),
                        )
                    },
                }
            ]
        return private_json(request, body)

    @app.get("/api/maps/{map_id}/tiles/vector/{version}/style.json")
    async def style(map_id: int, version: UUID, request: Request):
        async with app.state.pool.connection() as conn:
            await set_timeouts(conn)
            await authorize(conn, request, map_id, "metadata")
            row = await metadata_row(conn, map_id, version, "vector")
        return private_json(request, vector_style(row, descriptor(config, row, None, now())))

    @app.get("/api/maps/{map_id}/tiles/{version}/objects/{source_kind}/{source_id}")
    async def details(
        map_id: int, version: UUID, source_kind: str, source_id: int, request: Request
    ):
        async with app.state.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await set_timeouts(conn)
            await authorize(conn, request, map_id, "details")
            row = await metadata_row(conn, map_id, version)
            types = list(map(str, row["cable_types"] or []))
            extra_predicates = []
            extra_params = []
            for provider in PROVIDERS.values():
                if provider.object_type != "cable_line":
                    extra_predicates.append("(f.object_type=%s AND f.source_kind=ANY(%s))")
                    extra_params.extend([provider.object_type, list(provider.source_kinds)])
            extra_sql = " OR ".join(extra_predicates) or "false"
            feature = await (
                await conn.execute(
                    f"""SELECT f.source_kind,f.source_id,f.object_type,f.title,
                f.style,f.properties,f.min_zoom,f.length_m,ST_AsGeoJSON(f.geom)::jsonb AS geometry,
                COALESCE((SELECT array_agg(fl.layer_id ORDER BY fl.layer_id) FROM map_data.feature_layers fl
                  WHERE fl.dataset_id=f.dataset_id AND fl.source_kind=f.source_kind AND fl.source_id=f.source_id),'{{}}'::bigint[]) AS layer_ids,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('role',l.role,'targetKind',l.target_kind,'targetId',l.target_id))
                  FROM map_data.feature_links l WHERE l.dataset_id=f.dataset_id AND l.source_kind=f.source_kind AND l.source_id=f.source_id),'[]') AS links
                FROM map_data.features f WHERE f.dataset_id=%s AND f.source_kind=%s AND f.source_id=%s
                AND f.hidden=false AND f.min_zoom<=%s AND (
                  (f.object_type='cable_line' AND f.source_kind=ANY(%s) AND {cable_type_sql("f")}) OR
                  ({extra_sql}))""",
                    (
                        row["dataset_version"],
                        source_kind,
                        source_id,
                        row["display_maxzoom"],
                        list(CABLE_SOURCE_KINDS),
                        types,
                        types,
                        *extra_params,
                    ),
                )
            ).fetchone()
            if not feature:
                reject(404, "OBJECT_NOT_FOUND", "Object is unavailable under release rules")
            if feature["object_type"] == "cable_line":
                feature["cableEndpoints"] = await cable_endpoint_names(
                    conn, row["dataset_version"], feature
                )
        attributes = (
            render_attributes(feature)
            if source_kind in CABLE_SOURCE_KINDS
            else {"objectId": f"{source_kind}:{source_id}"}
        )
        return JSONResponse(
            {
                "tileVersion": str(version),
                "datasetVersion": str(row["dataset_version"]),
                "objectId": f"{source_kind}:{source_id}",
                **feature,
                "renderAttributes": attributes,
            },
            headers={"Cache-Control": "private, no-cache"},
        )

    @app.get("/tiles/maps/{map_id}/{format}/{version}/{z}/{x}/{filename}")
    async def xyz(
        map_id: int,
        format: Literal["vector", "raster"],
        version: UUID,
        z: int,
        x: int,
        filename: str,
        request: Request,
    ):
        age = verify_signature(config, map_id, format, version, request, now())
        match = re.fullmatch(r"([0-9]+)(@2x)?\.(pbf|webp)", filename)
        if not match:
            reject(422, "INVALID_TILE_COORDINATES", "Invalid tile filename")
        y = int(match[1])
        density = 2 if match[2] else 1
        if (
            map_id <= 0
            or (format == "vector" and (match[3] != "pbf" or density != 1))
            or (format == "raster" and match[3] != "webp")
        ):
            reject(422, "INVALID_TILE_COORDINATES", "Invalid format or density")
        try:
            tile_bounds(z, x, y)
        except ValueError:
            reject(422, "INVALID_TILE_COORDINATES", "Invalid XYZ")
        record = await cache.resolve(None, version, fetch=fetch)
        retained(record, config, map_id, format)
        try:
            payload = await run_in_threadpool(tile_payload, config, record, z, x, y, density)
        except (OSError, ValueError, KeyError, TileError):
            fresh = await cache.resolve(None, version, force=True, fetch=fetch)
            retained(fresh, config, map_id, format)
            reject(503, "TILE_FILE_MISSING", "Expected tile or manifest is unavailable")
        if format == "vector":
            payload = await run_in_threadpool(gzip.compress, payload, 6, mtime=0)
        etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
        # Mode B requires origin revalidation on CDN cache-hit. Browser max-age
        # remains bounded by signature expiry; signature is checked before 304.
        control = f"public, max-age={age}, immutable"
        if config.signing == "hmac":
            control += ", s-maxage=0, must-revalidate"
        headers = {"Cache-Control": control, "ETag": etag, "X-Content-Type-Options": "nosniff"}
        if format == "vector":
            headers["Content-Encoding"] = "gzip"
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            payload,
            media_type="application/vnd.mapbox-vector-tile" if format == "vector" else "image/webp",
            headers=headers,
        )
