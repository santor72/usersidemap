"""Actual HTTP contracts on disposable PostGIS and real vector/raster files."""

import gzip
import io
import json
import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_cable_render_database import published_fixture  # noqa: F401 -- fixture dependency
from test_vector_pipeline import tile_fixture as tile_fixture  # noqa: PLC0414 -- pytest re-export

from map_api.app import create_app
from map_api.tile_auth import issue_token
from map_api.tile_routes import file_index
from map_tiles.lifecycle import TileLock
from map_tiles.manifest import decode_vector
from map_tiles.pipeline import bake_raster, bake_vector
from map_tiles.publication import publish_locked, rollback_locked
from map_tiles.status_cache import VersionStatusCache, status_key


@pytest.fixture
def api_fixture(tile_fixture):
    conn, dataset, config = tile_fixture
    token = issue_token(conn, "fixture", [3], ["metadata", "details"], 600)
    clock = [2000000000]
    settings = {
        "database_url": config.database_url,
        "source_system": config.source_system,
        "tiles_root": str(config.root),
        "tiles_public_base": "http://testserver",
        "tiles_clock": lambda: clock[0],
        "redis_url": os.environ.get("TEST_MAP_REDIS_URL"),
    }
    try:
        yield conn, dataset, config, token, clock, settings
    finally:
        conn.execute("DELETE FROM map_data.api_tokens WHERE id=%s", (token["id"],))
        file_index.cache_clear()


def build_publish(conn, dataset, config, format="vector"):
    r = (bake_vector if format == "vector" else bake_raster)(replace(config, minzoom=14), dataset)
    with TileLock(conn, 3, format) as lease:
        publish_locked(config, lease, UUID(r["tileVersion"]))
    return r


def headers(token):
    return {"Authorization": "Bearer " + token["token"]}


def tile_url(report, format="vector", density=1):
    folder = report["_folder"]
    manifest = json.loads((folder / "manifest.json").read_text())
    entry = manifest["coverage"]["native"][str(manifest["maxzoom"])][0]
    return f"/tiles/maps/3/{format}/{report['tileVersion']}/{manifest['maxzoom']}/{entry['x']}/{entry['y']}{'@2x' if density == 2 else ''}.{'pbf' if format == 'vector' else 'webp'}"


def final_folder(config, report, format):
    return config.root / f"3/{format}/{report['tileVersion']}"


def test_auth_scopes_revocation_cold_start_etag_and_cors(api_fixture):
    conn, _, _, token, _, settings = api_fixture
    with TestClient(create_app(settings=settings)) as client:
        path = "/api/maps/3/tiles/catalog"
        assert client.get(path).status_code == 401
        response = client.get(path, headers=headers(token))
        assert response.status_code == 200
        assert response.json()["formats"] == {
            f: {"active": None, "prev": None} for f in ("vector", "raster")
        }
        assert (
            client.get(
                path, headers={**headers(token), "If-None-Match": response.headers["etag"]}
            ).status_code
            == 304
        )
        assert client.get("/api/maps/4/tiles/catalog", headers=headers(token)).status_code == 403
        preflight = client.options(
            path,
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,if-none-match",
            },
        )
        assert preflight.status_code == 200
        assert (
            "etag"
            in client.get(path, headers={**headers(token), "Origin": "http://localhost:3000"})
            .headers["access-control-expose-headers"]
            .lower()
        )
        conn.execute(
            "UPDATE map_data.api_tokens SET revoked_at=clock_timestamp() WHERE id=%s",
            (token["id"],),
        )
        assert client.get(path, headers=headers(token)).status_code == 401


def test_catalog_tilejson_style_native_gzip_empty_and_retina(api_fixture):
    conn, dataset, config, token, _, settings = api_fixture
    vector = build_publish(conn, dataset, config)
    raster = build_publish(conn, dataset, config, "raster")
    vector["_folder"] = final_folder(config, vector, "vector")
    raster["_folder"] = final_folder(config, raster, "raster")
    captured = []
    app = create_app(settings=settings)

    async def tap(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.body":
                captured.append(message.get("body", b""))
            await send(message)

        await app(scope, receive, capture)

    with TestClient(tap) as client:
        catalog = client.get("/api/maps/3/tiles/catalog", headers=headers(token)).json()
        assert catalog["tiles_lag"] == "banner" and catalog["catalogPollSeconds"] == 0
        for format, native, size in (("vector", 14, 512), ("raster", 15, 256)):
            item = catalog["formats"][format]["active"]
            assert (
                item["datasetVersion"] == str(dataset)
                and item["maxzoom"] == native
                and item["tileSize"] == size
            )
            tj = client.get(item["tilejsonUrl"], headers=headers(token))
            assert tj.status_code == 200
            assert tj.json()["maxzoom"] == native and tj.json()["displayMaxzoom"] == 18
            if format == "raster":
                assert "@2x.webp" in tj.json()["template2x"]
            else:
                style = client.get(item["styleUrl"], headers=headers(token)).json()
                assert style["sources"]["cables"]["promoteId"] == "objectId"
                assert all(l["source-layer"] == "cable_line" for l in style["layers"])
        captured.clear()
        response = client.get(tile_url(vector))
        assert response.status_code == 200 and response.headers["content-encoding"] == "gzip"
        assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
        assert decode_vector(gzip.decompress(b"".join(captured)))
        assert decode_vector(response.content)
        empty = client.get(f"/tiles/maps/3/vector/{vector['tileVersion']}/14/0/0.pbf")
        assert empty.status_code == 200 and decode_vector(empty.content) == []
        over = client.get(f"/tiles/maps/3/vector/{vector['tileVersion']}/15/0/0.pbf")
        assert over.status_code == 422 and over.json()["error"]["code"] == "TILE_OVERZOOM"
        assert (
            client.get(f"/tiles/maps/3/vector/{vector['tileVersion']}/14/0/0@2x.pbf").status_code
            == 422
        )
        for density in (1, 2):
            response = client.get(tile_url(raster, "raster", density))
            assert response.status_code == 200
            assert (
                response.headers["content-type"] == "image/webp"
                and "content-encoding" not in response.headers
            )
            with Image.open(io.BytesIO(response.content)) as im:
                assert im.size == (256 * density, 256 * density)
            empty = client.get(
                f"/tiles/maps/3/raster/{raster['tileVersion']}/15/0/0{'@2x' if density == 2 else ''}.webp"
            )
            with Image.open(io.BytesIO(empty.content)) as im:
                assert im.convert("RGBA").getbbox() is None
        assert (
            client.get(f"/tiles/maps/4/vector/{vector['tileVersion']}/14/0/0.pbf").status_code
            == 404
        )
        assert client.get(f"/tiles/maps/3/vector/{uuid4()}/14/0/0.pbf").status_code == 404
        assert (
            client.get(f"/tiles/maps/3/vector/{vector['tileVersion']}/14/-1/0.pbf").status_code
            == 422
        )
        assert client.get(
            f"/tiles/maps/3/vector/{vector['tileVersion']}/14/0/../../manifest.json"
        ).status_code in (404, 422)


def test_hmac_prefix_densities_expiry_etag_and_cache_bound(api_fixture):
    conn, dataset, config, token, clock, settings = api_fixture
    report = build_publish(conn, dataset, config, "raster")
    settings.update(
        tiles_signing="hmac", tiles_hmac_secret="test-secret-" * 4, tiles_signature_ttl=60
    )
    with TestClient(create_app(settings=settings)) as client:
        path = "/api/maps/3/tiles/catalog"
        first = client.get(path, headers=headers(token))
        item = first.json()["formats"]["raster"]["active"]
        manifest = json.loads(
            (final_folder(config, report, "raster") / "manifest.json").read_text()
        )
        e = manifest["coverage"]["native"]["15"][0]
        url = (
            item["template"]
            .replace("{z}", "15")
            .replace("{x}", str(e["x"]))
            .replace("{y}", str(e["y"]))
        )
        url2 = (
            item["template2x"]
            .replace("{z}", "15")
            .replace("{x}", str(e["x"]))
            .replace("{y}", str(e["y"]))
        )
        response = client.get(url)
        assert response.status_code == 200
        assert (
            "max-age=60" in response.headers["cache-control"]
            and "s-maxage=0" in response.headers["cache-control"]
        )
        assert client.get(url2).status_code == 200
        assert (
            client.get(url.replace("/raster/", "/vector/")).json()["error"]["code"]
            == "TILE_SIGNATURE_INVALID"
        )
        unsigned = url.split("?")[0]
        assert client.get(unsigned).json()["error"]["code"] == "TILE_SIGNATURE_INVALID"
        clock[0] += 1
        second = client.get(
            path, headers={**headers(token), "If-None-Match": first.headers["etag"]}
        )
        assert second.status_code == 200 and second.headers["etag"] != first.headers["etag"]
        assert second.json()["formats"]["raster"]["active"]["tileVersion"] == report["tileVersion"]
        clock[0] += 60
        expired = client.get(url, headers={"If-None-Match": response.headers["etag"]})
        assert (
            expired.status_code == 403
            and expired.json()["error"]["code"] == "TILE_SIGNATURE_EXPIRED"
        )
        assert expired.headers["cache-control"] == "no-store"


def test_details_and_pin_use_release_dataset_and_none_has_no_pin(api_fixture):
    conn, dataset, config, token, _, settings = api_fixture
    report = build_publish(conn, dataset, config)
    with TestClient(create_app(settings=settings)) as client:
        base = f"/api/maps/3/tiles/{report['tileVersion']}/objects"
        assert client.get(base + "/cable/1").status_code == 401
        detail = client.get(base + "/cable/1", headers=headers(token))
        assert detail.status_code == 200
        assert detail.json()["cableEndpoints"] == [None, None]
        assert (
            detail.json()["datasetVersion"] == str(dataset)
            and detail.json()["geometry"]["type"] == "LineString"
        )
        assert client.get(base + "/cable_group/5", headers=headers(token)).status_code == 200
        assert client.get(base + "/cable/4", headers=headers(token)).status_code == 404
        assert client.get(base + "/cable/6", headers=headers(token)).status_code == 404
        body = {
            "mapId": 3,
            "bbox": [36.9, 54.9, 37.1, 55.1],
            "zoom": 14,
            "objectTypes": ["cable_line", "node"],
            "pin": {"tileVersion": report["tileVersion"], "format": "vector"},
        }
        assert client.post("/api/map/objects/preload", json=body).status_code == 401
        pin = client.post("/api/map/objects/preload", json=body, headers=headers(token))
        assert pin.status_code == 200, pin.text
        assert pin.json()["datasetVersion"] == str(dataset) and pin.json()["totalObjects"] == 0
        lease = conn.execute(
            "SELECT query FROM map_data.query_leases WHERE id=%s", (pin.json()["queryId"],)
        ).fetchone()["query"]
        assert (
            lease["objectTypes"] == ["node"]
            and lease["pin"]["tileVersion"] == report["tileVersion"]
        )
        body.pop("pin")
        none = client.post("/api/map/objects/preload", json=body)
        assert none.status_code == 200 and none.json()["totalObjects"] > 0
        wrong = {**body, "pin": {"tileVersion": str(uuid4()), "format": "vector"}}
        assert (
            client.post("/api/map/objects/preload", json=wrong, headers=headers(token)).status_code
            == 404
        )


def test_missing_file_after_stale_ready_forces_db_410_or_503(api_fixture):
    conn, dataset, config, _token, _, settings = api_fixture
    url = os.environ.get("TEST_MAP_REDIS_URL")
    if not url:
        pytest.skip("Disposable Redis required")
    cache = VersionStatusCache.from_url(url)
    first = build_publish(conn, dataset, config)
    first["_folder"] = final_folder(config, first, "vector")
    path = tile_url(first)
    with TestClient(create_app(settings=settings)) as client:
        assert client.get(path).status_code == 200
        ready = cache.resolve(conn, UUID(first["tileVersion"]))
        build_publish(conn, dataset, config)
        third = build_publish(conn, dataset, config)
        cache.client.set(status_key(UUID(first["tileVersion"])), json.dumps(ready), ex=30)
        retired = client.get(path)
        assert (
            retired.status_code == 410 and retired.json()["error"]["code"] == "TILE_VERSION_RETIRED"
        )
        assert retired.headers["cache-control"] == "no-store"
        third["_folder"] = final_folder(config, third, "vector")
        third_path = tile_url(third)
        assert client.get(third_path).status_code == 200
        relative = third_path.split("/" + third["tileVersion"] + "/")[1]
        (third["_folder"] / relative).unlink()
        lost = client.get(third_path)
        assert lost.status_code == 503 and lost.json()["error"]["code"] == "TILE_FILE_MISSING"
    cache.close()


def test_redis_hit_has_no_database_and_outage_falls_back(api_fixture):
    conn, dataset, config, _, _, settings = api_fixture
    if not os.environ.get("TEST_MAP_REDIS_URL"):
        pytest.skip("Disposable Redis required")
    r = build_publish(conn, dataset, config)
    r["_folder"] = final_folder(config, r, "vector")
    app = create_app(settings=settings)

    class UnavailablePool:
        def connection(self):
            raise AssertionError("A hot Redis hit borrowed a DB connection")

    with TestClient(app) as client:
        path = tile_url(r)
        assert client.get(path).status_code == 200
        pool = app.state.pool
        app.state.pool = UnavailablePool()
        try:
            assert client.get(path).status_code == 200
        finally:
            app.state.pool = pool
    settings["redis_url"] = "redis://127.0.0.1:1/0"
    with TestClient(create_app(settings=settings)) as client:
        assert client.get(tile_url(r)).status_code == 200


def test_scope_details_and_expired_token_rejected(api_fixture):
    conn, dataset, config, _, _, settings = api_fixture
    report = build_publish(conn, dataset, config)
    metadata = issue_token(conn, "metadata-only", [3], ["metadata"], 600)
    try:
        with TestClient(create_app(settings=settings)) as client:
            path = f"/api/maps/3/tiles/{report['tileVersion']}/objects/cable/1"
            assert client.get(path, headers=headers(metadata)).status_code == 403
            conn.execute(
                "UPDATE map_data.api_tokens SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (metadata["id"],),
            )
            assert (
                client.get("/api/maps/3/tiles/catalog", headers=headers(metadata)).status_code
                == 401
            )
    finally:
        conn.execute("DELETE FROM map_data.api_tokens WHERE id=%s", (metadata["id"],))


@pytest.mark.parametrize("tile_fixture", ["with-node"], indirect=True)
def test_http_rollback_provenance_pin_points_batch_auth_and_lag(api_fixture):
    conn, dataset, config, token, _, settings = api_fixture
    old = build_publish(conn, dataset, config)
    run, new_dataset = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO map_data.sync_runs (id,source_system,map_id,status,rules_version) VALUES (%s,%s,3,'succeeded','fixture')",
        (run, config.source_system),
    )
    conn.execute(
        "INSERT INTO map_data.datasets (id,run_id,source_system,map_id,status) VALUES (%s,%s,%s,3,'building')",
        (new_dataset, run, config.source_system),
    )
    conn.execute("UPDATE map_data.datasets SET status='ready' WHERE id=%s", (new_dataset,))
    build_publish(conn, new_dataset, config)
    conn.execute(
        "UPDATE map_data.active_datasets SET dataset_id=%s WHERE source_system=%s",
        (new_dataset, config.source_system),
    )
    body = {
        "mapId": 3,
        "bbox": [36.9, 54.9, 37.1, 55.1],
        "zoom": 14,
        "objectTypes": ["cable_line", "node"],
    }
    try:
        with TestClient(create_app(settings=settings)) as client:
            before = client.get("/api/maps/3/tiles/catalog", headers=headers(token)).json()
            assert before["formats"]["vector"]["active"]["datasetVersion"] == str(new_dataset)
            with TileLock(conn, 3, "vector") as lease:
                rollback_locked(config, lease, target=UUID(old["tileVersion"]))
            after = client.get("/api/maps/3/tiles/catalog", headers=headers(token)).json()
            assert after["formats"]["vector"]["active"]["datasetVersion"] == str(dataset)
            assert after["formats"]["vector"]["active"]["tilesBehindActive"]
            assert after["formats"]["vector"]["prev"]["datasetVersion"] == str(new_dataset)
            assert client.post("/api/map/objects/preload", json=body).json()["totalObjects"] == 0
            pin = {**body, "pin": {"format": "vector", "tileVersion": old["tileVersion"]}}
            preload = client.post("/api/map/objects/preload", json=pin, headers=headers(token))
            assert preload.status_code == 200, preload.text
            assert (
                preload.json()["datasetVersion"] == str(dataset)
                and preload.json()["totalObjects"] == 1
            )
            qid = preload.json()["queryId"]
            batch = {"queryId": qid, "batchIndex": 0}
            assert client.post("/api/map/objects/batch", json=batch).status_code == 401
            loaded = client.post("/api/map/objects/batch", json=batch, headers=headers(token))
            assert loaded.status_code == 200
            assert loaded.json()["datasetVersion"] == str(dataset)
            detail = f"/api/map/objects/node/100?queryId={qid}"
            assert client.get(detail).status_code == 401
            assert client.get(detail, headers=headers(token)).status_code == 200
            tile_point = client.get(
                f"/api/maps/3/tiles/{old['tileVersion']}/objects/node/100", headers=headers(token)
            )
            assert tile_point.status_code == 200, tile_point.text
            assert tile_point.json()["geometry"]["type"] == "Point"
            conn.execute(
                "UPDATE map_data.api_tokens SET revoked_at=clock_timestamp() WHERE id=%s",
                (token["id"],),
            )
            assert (
                client.post(
                    "/api/map/objects/batch", json=batch, headers=headers(token)
                ).status_code
                == 401
            )
    finally:
        with conn.transaction():
            conn.execute(
                "DELETE FROM map_data.active_tile_versions WHERE source_system=%s",
                (config.source_system,),
            )
            conn.execute(
                "DELETE FROM map_data.tile_versions WHERE source_system=%s", (config.source_system,)
            )
            conn.execute(
                "DELETE FROM map_data.tile_builds WHERE source_system=%s", (config.source_system,)
            )
            conn.execute(
                "DELETE FROM map_data.active_datasets WHERE source_system=%s",
                (config.source_system,),
            )
            conn.execute("DELETE FROM map_data.query_leases WHERE dataset_id=%s", (new_dataset,))
            conn.execute("DELETE FROM map_data.datasets WHERE id=%s", (new_dataset,))
            conn.execute("DELETE FROM map_data.sync_runs WHERE id=%s", (run,))


def test_real_readonly_api_role_and_cookie_session(api_fixture):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from map_sync.grants import grant_tile_roles

    conn, dataset, config, token, _, settings = api_fixture
    report = build_publish(conn, dataset, config)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='map_api'").fetchone():
        conn.execute("CREATE ROLE map_api")
    conn.execute("ALTER ROLE map_api LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'api-test-local'")
    conn.execute("GRANT USAGE ON SCHEMA map_data TO map_api")
    conn.execute(
        "GRANT SELECT ON map_data.datasets,map_data.active_datasets,map_data.features,map_data.layers,map_data.feature_layers,map_data.feature_links TO map_api"
    )
    conn.execute(
        "GRANT SELECT,INSERT,UPDATE,DELETE ON map_data.query_leases,map_data.query_batches,map_data.query_items,map_data.query_lease_totals TO map_api"
    )
    conn.execute(
        "GRANT EXECUTE ON FUNCTION map_data.api_pin_active_dataset(text,bigint) TO map_api"
    )
    grant_tile_roles(conn)
    args = conninfo_to_dict(config.database_url)
    args.update(user="map_api", password="api-test-local")
    settings["database_url"] = make_conninfo(**args)
    with TestClient(create_app(settings=settings)) as client:
        client.cookies.set("map_tiles_session", token["token"])
        assert client.get("/api/maps/3/tiles/catalog").status_code == 200
        assert (
            client.get(
                "/api/maps/3/tiles/catalog", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
        body = {
            "mapId": 3,
            "bbox": [36.9, 54.9, 37.1, 55.1],
            "zoom": 14,
            "objectTypes": ["cable_line"],
            "pin": {"format": "vector", "tileVersion": report["tileVersion"]},
        }
        response = client.post("/api/map/objects/preload", json=body)
        assert response.status_code == 200, response.text
        assert (
            response.json()["datasetVersion"] == str(dataset)
            and response.json()["totalObjects"] == 0
        )
    with psycopg.connect(**args, autocommit=True) as readonly:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            readonly.execute("DELETE FROM map_data.api_tokens")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            readonly.execute("UPDATE map_data.tile_versions SET cleanup_status='error'")
