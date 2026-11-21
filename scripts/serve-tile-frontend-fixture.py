#!/usr/bin/env python3
"""Disposable browser fixture; local *_test DB only. Never production."""

import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID


def main():
    ROOT = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
    from urllib.parse import urlparse

    url = os.environ.get("TEST_MAP_DB_URL", "")
    parsed = urlparse(url)
    if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith(
        "_test"
    ):
        raise SystemExit(
            "Set TEST_MAP_DB_URL to a local disposable database ending in _test"
        )
    import psycopg
    import uvicorn

    for attempt in range(50):
        try:
            with psycopg.connect(os.environ["TEST_MAP_DB_URL"]):
                pass
            break
        except psycopg.OperationalError:
            time.sleep(0.2)
    import test_cable_render_database as fixture

    fixture.FIXTURE["features"] = [
        f for f in fixture.FIXTURE["features"] if f["source_id"] != 8
    ]
    fixture.FIXTURE["features"].append(
        {
            "source_kind": "node",
            "source_id": 100,
            "object_type": "node",
            "geometry": {"type": "Point", "coordinates": [37, 55]},
            "min_zoom": 1,
            "hidden": False,
            "style": {},
            "properties": {},
            "layer_ids": [1],
        }
    )
    generator = fixture.published_fixture.__wrapped__()
    conn, dataset, source, url = next(generator)
    import tempfile

    from map_api.app import create_app
    from map_api.tile_auth import issue_token
    from map_tiles.config import TileConfig
    from map_tiles.lifecycle import TileLock
    from map_tiles.pipeline import bake_raster, bake_vector
    from map_tiles.publication import publish_locked

    folder = tempfile.TemporaryDirectory(prefix="map-tiles-frontend-")
    config = TileConfig(url, Path(folder.name), source_system=source)
    r = bake_vector(config, dataset)
    with TileLock(conn, 3, "vector") as lock:
        publish_locked(config, lock, UUID(r["tileVersion"]))
    raster_dataset = raster_run = None
    if os.environ.get("TILE_FRONTEND_RASTER") == "1":
        from uuid import uuid4

        raster_dataset, raster_run = uuid4(), uuid4()
        with conn.transaction():
            conn.execute(
                "INSERT INTO map_data.sync_runs(id,source_system,map_id,status,rules_version) VALUES(%s,%s,3,'succeeded','fixture')",
                (raster_run, source),
            )
            conn.execute(
                "INSERT INTO map_data.datasets(id,run_id,source_system,map_id,status) VALUES(%s,%s,%s,3,'building')",
                (raster_dataset, raster_run, source),
            )
            conn.execute(
                "INSERT INTO map_data.layers(dataset_id,source_id,title,default_visible) SELECT %s,source_id,title,default_visible FROM map_data.layers WHERE dataset_id=%s",
                (raster_dataset, dataset),
            )
            conn.execute(
                "INSERT INTO map_data.features(dataset_id,source_kind,source_id,object_type,title,geom,min_zoom,hidden,vertex_count,style,properties,length_m) SELECT %s,source_kind,CASE WHEN source_kind='node' THEN 101 ELSE source_id END,object_type,title,geom,min_zoom,hidden,vertex_count,style,properties,length_m FROM map_data.features WHERE dataset_id=%s",
                (raster_dataset, dataset),
            )
            conn.execute(
                "INSERT INTO map_data.feature_layers(dataset_id,source_kind,source_id,layer_id) SELECT %s,source_kind,CASE WHEN source_kind='node' THEN 101 ELSE source_id END,layer_id FROM map_data.feature_layers WHERE dataset_id=%s",
                (raster_dataset, dataset),
            )
            conn.execute(
                "UPDATE map_data.datasets SET status='ready' WHERE id=%s",
                (raster_dataset,),
            )
        raster = bake_raster(config, raster_dataset)
        with TileLock(conn, 3, "raster") as lock:
            publish_locked(config, lock, UUID(raster["tileVersion"]))
    token = issue_token(conn, "frontend fixture", [3], ["metadata", "details"], 172800)
    p = Path("/tmp/map-tiles-frontend-private.json")
    p.write_text(
        json.dumps(
            {
                "token": token["token"],
                "source": source,
                "dataset": str(dataset),
                "version": r["tileVersion"],
            }
        )
    )
    p.chmod(0o600)
    # A newer source snapshot demonstrates lag while retaining the same fixture objects.
    from uuid import uuid4

    run, new = uuid4(), uuid4()
    with conn.transaction():
        conn.execute(
            "INSERT INTO map_data.sync_runs(id,source_system,map_id,status,rules_version) VALUES(%s,%s,3,'succeeded','fixture')",
            (run, source),
        )
        conn.execute(
            "INSERT INTO map_data.datasets(id,run_id,source_system,map_id,status) VALUES(%s,%s,%s,3,'building')",
            (new, run, source),
        )
        conn.execute("UPDATE map_data.datasets SET status='ready' WHERE id=%s", (new,))
        conn.execute(
            "UPDATE map_data.active_datasets SET dataset_id=%s WHERE source_system=%s",
            (new, source),
        )
    settings = {
        "database_url": url,
        "source_system": source,
        "tiles_root": str(config.root),
        "tiles_public_base": "http://localhost:8009",
        "cors_origins": "http://localhost:3019",
        "tiles_signing": os.environ.get("TILE_FRONTEND_SIGNING", "none"),
        "tiles_hmac_secret": "disposable-frontend-hmac-secret-32-characters",
        "tiles_signature_ttl": int(os.environ.get("TILE_FRONTEND_SIGNATURE_TTL", "10")),
    }
    print(
        "Disposable vector fixture ready; secrets kept in private temp file", flush=True
    )
    app = create_app(settings=settings)

    @app.post("/fixture/advance-vector")
    async def advance_vector():
        for _ in range(2):
            report = bake_vector(config, dataset)
            with TileLock(conn, 3, "vector") as lock:
                publish_locked(config, lock, UUID(report["tileVersion"]))
        return {"tileVersion": report["tileVersion"]}

    try:
        uvicorn.run(app, host="127.0.0.1", port=8009, access_log=False)
    finally:
        with conn.transaction():
            conn.execute("DELETE FROM map_data.api_tokens WHERE id=%s", (token["id"],))
            conn.execute(
                "DELETE FROM map_data.active_tile_versions WHERE source_system=%s",
                (source,),
            )
            conn.execute(
                "DELETE FROM map_data.tile_versions WHERE source_system=%s", (source,)
            )
            conn.execute(
                "DELETE FROM map_data.tile_builds WHERE source_system=%s", (source,)
            )
            conn.execute(
                "DELETE FROM map_data.active_datasets WHERE source_system=%s", (source,)
            )
            conn.execute(
                "DELETE FROM map_data.query_leases WHERE dataset_id=%s", (new,)
            )
            conn.execute("DELETE FROM map_data.datasets WHERE id=%s", (new,))
            conn.execute("DELETE FROM map_data.sync_runs WHERE id=%s", (run,))
            if raster_dataset:
                conn.execute(
                    "DELETE FROM map_data.query_leases WHERE dataset_id=%s",
                    (raster_dataset,),
                )
                conn.execute(
                    "DELETE FROM map_data.datasets WHERE id=%s", (raster_dataset,)
                )
                conn.execute(
                    "DELETE FROM map_data.sync_runs WHERE id=%s", (raster_run,)
                )
        generator.close()
        p.unlink(missing_ok=True)
        folder.cleanup()


if __name__ == "__main__":
    main()
