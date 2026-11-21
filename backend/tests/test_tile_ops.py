"""Durable operations metrics on real PostGIS; no dependency on worker logs."""

from uuid import uuid4

from fastapi.testclient import TestClient
from test_cable_render_database import published_fixture as published_fixture  # noqa: PLC0414
from test_tile_api import api_fixture as api_fixture  # noqa: PLC0414
from test_tile_api import build_publish
from test_vector_pipeline import tile_fixture as tile_fixture  # noqa: PLC0414

from map_api.app import create_app
from map_tiles.lifecycle import BuildSpec, TileLock


def value(text, name, format=None):
    lines = [line for line in text.splitlines() if line.startswith(f"map_tiles_{name}")]
    if format:
        lines = [line for line in lines if f'format="{format}"' in line]
    assert len(lines) == 1
    return float(lines[0].rsplit(" ", 1)[1])


def test_metrics_cold_start_publish_and_failed_build(api_fixture):
    conn, dataset, config, _, _, settings = api_fixture
    settings["tiles_lag_grace_seconds"] = 180
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert value(response.text, "lag_grace_seconds") == 180
        for format in ("vector", "raster"):
            assert value(response.text, "behind", format) == 1
            assert value(response.text, "retained_versions", format) == 0
            assert value(response.text, "latest_build_failed", format) == 0
        build_publish(conn, dataset, config)
        text = client.get("/metrics").text
        assert value(text, "behind", "vector") == 0
        assert value(text, "lag_seconds", "vector") == 0
        assert value(text, "retained_versions", "vector") == 1
        assert value(text, "behind", "raster") == 1
        with TileLock(conn, 3, "vector") as lease:
            row = conn.execute(
                "SELECT * FROM map_data.tile_versions WHERE source_system=%s AND status='ready'",
                (config.source_system,),
            ).fetchone()
            build = lease.start(
                BuildSpec(
                    config.source_system,
                    3,
                    "vector",
                    dataset,
                    row["rules_version"],
                    row["rules_hash"],
                    row["geometry_profile"],
                    row["tolerance_hash"],
                    14,
                    14,
                    18,
                    {},
                )
            )
            lease.fail(build, "TEST_FAILURE")
        text = client.get("/metrics").text
        assert value(text, "latest_build_failed", "vector") == 1
        assert value(text, "behind", "vector") == 0
        assert value(text, "latest_build_seconds", "vector") >= 0
        assert "postgresql://" not in text and "Authorization" not in text
        assert "tileVersion" not in text and "?sig=" not in text


def test_redis_outage_does_not_change_readiness(api_fixture):
    _, _, _, _, _, settings = api_fixture
    settings["redis_url"] = "redis://127.0.0.1:1/0"
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/health/ready").status_code == 200
        text = client.get("/metrics").text
        assert value(text, "redis_enabled") == 1
        assert value(text, "redis_up") == 0


def test_lag_is_since_new_active_snapshot_until_publish(api_fixture):
    conn, dataset, config, _, _, settings = api_fixture
    build_publish(conn, dataset, config)
    run, newer = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO map_data.sync_runs(id,source_system,map_id,status,rules_version) VALUES (%s,%s,3,'succeeded','test')",
        (run, config.source_system),
    )
    conn.execute(
        "INSERT INTO map_data.datasets(id,run_id,source_system,map_id,status,published_at) VALUES (%s,%s,%s,3,'building',clock_timestamp()-interval '5 minutes')",
        (newer, run, config.source_system),
    )
    conn.execute("UPDATE map_data.datasets SET status='ready' WHERE id=%s", (newer,))
    conn.execute(
        "UPDATE map_data.active_datasets SET dataset_id=%s WHERE source_system=%s",
        (newer, config.source_system),
    )
    try:
        with TestClient(create_app(settings=settings)) as client:
            text = client.get("/metrics").text
            assert value(text, "lag_grace_seconds") == 210
            assert value(text, "behind", "vector") == 1
            assert value(text, "lag_seconds", "vector") >= 300
            build_publish(conn, newer, config)
            text = client.get("/metrics").text
            assert value(text, "behind", "vector") == 0
            assert value(text, "lag_seconds", "vector") == 0
            assert value(text, "retained_versions", "vector") == 2
            assert value(text, "lag_seconds", "raster") >= 300
    finally:
        conn.execute(
            "UPDATE map_data.active_datasets SET dataset_id=%s WHERE source_system=%s",
            (dataset, config.source_system),
        )
