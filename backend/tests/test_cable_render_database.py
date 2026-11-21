"""Parity gate against disposable PostGIS, including the actual object API."""

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_cable_render import FIXTURE

from map_api.app import create_app
from map_render.cables import cable_type_sql, layer_filter_sql, visible_sql
from map_sync.migrate import migrate
from map_tiles.geometry import EXTENT, WORLD, xyz
from map_tiles.planning import plan_zoom, selection_zoom
from map_tiles.vector import EMPTY_MVT, encode_mvt, tile_inputs


@pytest.fixture
def published_fixture():
    url = os.environ.get("TEST_MAP_DB_URL")
    if not url:
        pytest.skip("TEST_MAP_DB_URL is required for disposable PostGIS tests")
    run, dataset = uuid4(), uuid4()
    source = "tiles_fixture_" + run.hex
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        migrate(conn)
        with conn.transaction():
            conn.execute(
                """INSERT INTO map_data.sync_runs
                (id,source_system,map_id,status,rules_version)
                VALUES (%s,%s,3,'succeeded','fixture')""",
                (run, source),
            )
            conn.execute(
                """INSERT INTO map_data.datasets
                (id,run_id,source_system,map_id,status) VALUES (%s,%s,%s,3,'building')""",
                (dataset, run, source),
            )
            for layer in (1, 11):
                conn.execute(
                    """INSERT INTO map_data.layers
                    (dataset_id,source_id,title,default_visible)
                    VALUES (%s,%s,'fixture',true)""",
                    (dataset, layer),
                )
            for feature in FIXTURE["features"]:
                conn.execute(
                    """INSERT INTO map_data.features
                    (dataset_id,source_kind,source_id,object_type,geom,min_zoom,hidden,
                     vertex_count,style,properties)
                    VALUES (%s,%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),
                            %s,%s,4,%s,%s)""",
                    (
                        dataset,
                        feature["source_kind"],
                        feature["source_id"],
                        feature["object_type"],
                        Jsonb(feature["geometry"]),
                        feature["min_zoom"],
                        feature["hidden"],
                        Jsonb(feature["style"]),
                        Jsonb(feature["properties"]),
                    ),
                )
                for layer in feature["layer_ids"]:
                    conn.execute(
                        "INSERT INTO map_data.feature_layers VALUES (%s,%s,%s,%s)",
                        (dataset, feature["source_kind"], feature["source_id"], layer),
                    )
            conn.execute("UPDATE map_data.datasets SET status='ready' WHERE id=%s", (dataset,))
            conn.execute("INSERT INTO map_data.active_datasets VALUES (%s,3,%s)", (source, dataset))
        try:
            yield conn, dataset, source, url
        finally:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM map_data.active_datasets WHERE source_system=%s", (source,)
                )
                conn.execute("DELETE FROM map_data.query_leases WHERE dataset_id=%s", (dataset,))
                conn.execute("DELETE FROM map_data.datasets WHERE id=%s", (dataset,))
                conn.execute("DELETE FROM map_data.sync_runs WHERE id=%s", (run,))


@pytest.mark.parametrize(
    "zoom,layers,expected",
    [
        (13, None, {"cable:2", "cable:3", "cable_group:5"}),
        (14, None, {"cable:1", "cable:2", "cable:3", "cable_group:5", "cable:7"}),
        (14, [1], {"cable:1", "cable:3", "cable_group:5"}),
        (14, [11], {"cable:2", "cable:3", "cable_group:5"}),
    ],
)
def test_sql_and_http_match_original_selection(published_fixture, zoom, layers, expected):
    conn, dataset, source, url = published_fixture
    spatial = "ST_Intersects(f.geom,ST_MakeEnvelope(36.9,54.9,37.1,55.1,4326))"
    common = f"""SELECT f.source_kind||':'||f.source_id AS id FROM map_data.features f
        WHERE f.dataset_id=%s AND f.object_type='cable_line'
        AND f.source_kind=ANY(%s) AND {{visibility}} AND {{types}}
        AND {spatial}"""
    old = common.format(
        visibility="f.hidden=false AND f.min_zoom<=%s",
        types=("(f.properties->>'cableTypeId'=ANY(%s) OR f.properties->'cableTypeIds' ?| %s)"),
    )
    new = common.format(visibility=visible_sql(), types=cable_type_sql("f"))
    params = [dataset, ["cable", "cable_group"], zoom, ["1"], ["1"]]
    if layers:
        old += " AND " + layer_filter_sql()
        new += " AND " + layer_filter_sql()
        params.append(layers)
    for query in (old, new):
        assert {r["id"] for r in conn.execute(query, params)} == expected
    app = create_app(settings={"database_url": url, "source_system": source})
    with TestClient(app) as client:
        response = client.post(
            "/api/map/objects/preload",
            json={
                "mapId": 3,
                "bbox": FIXTURE["bbox"],
                "zoom": zoom,
                "objectTypes": ["cable_line"],
                "layerIds": layers,
            },
        )
        assert response.status_code == 200, response.text
        manifest = response.json()
        ids = set()
        for batch in manifest["batches"]:
            result = client.post(
                "/api/map/objects/batch",
                json={
                    "queryId": manifest["queryId"],
                    "batchIndex": batch["index"],
                },
            )
            assert result.status_code == 200, result.text
            ids.update(str(f["id"]) for f in result.json()["featureCollection"]["features"])
        assert ids == expected


def test_mvt_short_cable_group_attributes_and_empty_layer(published_fixture):
    from mapbox_vector_tile import decode

    conn, dataset, _, _ = published_fixture
    x, y = xyz(37, 55, 14)
    inputs = tile_inputs(conn, dataset, 14, x, y)
    ids = {f["attributes"]["objectId"] for f in inputs}
    assert "cable:1" in ids
    assert "cable_group:5" in ids
    decoded = decode(encode_mvt(conn, inputs, 14, x, y))["cable_line"]
    properties = [f["properties"] for f in decoded["features"]]
    assert {p["objectId"] for p in properties} == ids
    assert next(p for p in properties if p["objectId"] == "cable_group:5")["layerIds"] == ",1,11,"
    for payload in (EMPTY_MVT, encode_mvt(conn, [], 14, x, y)):
        empty = decode(payload)["cable_line"]
        assert empty["features"] == []
        assert empty["extent"] == 4096


def test_quantization_retains_input_ids_at_low_zoom(published_fixture):
    from mapbox_vector_tile import decode

    conn, dataset, _, _ = published_fixture
    x, y = xyz(37, 55, 1)
    inputs = tile_inputs(conn, dataset, 1, x, y)
    assert inputs
    features = decode(encode_mvt(conn, inputs, 1, x, y))["cable_line"]["features"]
    assert {f["properties"]["objectId"] for f in features} == {
        f["attributes"]["objectId"] for f in inputs
    }
    assert all(f["geometry"]["type"] in ("LineString", "MultiLineString") for f in features)


def test_raster_xyz_offset_and_short_cable_visibility(published_fixture):
    conn, dataset, _, _ = published_fixture
    assert selection_zoom("vector", 14) == 14
    assert selection_zoom("raster", 15) == 14
    for z, expected in ((14, False), (15, True)):
        x, y = xyz(37, 55, z)
        inputs = tile_inputs(conn, dataset, z, x, y, selection_zoom=selection_zoom("raster", z))
        assert ("cable:1" in {f["attributes"]["objectId"] for f in inputs}) == expected


def test_tile_edge_dateline_and_mercator_latitude(published_fixture):
    from mapbox_vector_tile import decode

    conn, dataset, _, _ = published_fixture
    for x, y, id in (
        (9875, 5182, "cable:7"),
        (9876, 5182, "cable:7"),
        (0, 8192, "cable:8"),
        (16383, 8192, "cable:8"),
        (8192, 0, "cable:9"),
    ):
        inputs = tile_inputs(conn, dataset, 14, x, y)
        assert id in {f["attributes"]["objectId"] for f in inputs}
        for feature in inputs:
            geometry = feature["geometry"]
            lines = (
                geometry["coordinates"]
                if geometry["type"] == "MultiLineString"
                else [geometry["coordinates"]]
            )
            assert all(abs(position[1]) <= WORLD / 2 + 1e-7 for line in lines for position in line)
        features = decode(
            encode_mvt(conn, inputs, 14, x, y), default_options={"y_coord_down": True}
        )["cable_line"]["features"]
        assert id in {f["properties"]["objectId"] for f in features}
        for feature in features:
            coordinates = feature["geometry"]["coordinates"]
            lines = (
                coordinates if feature["geometry"]["type"] == "MultiLineString" else [coordinates]
            )
            assert all(
                -128 <= v <= EXTENT + 128 for line in lines for position in line for v in position
            )
    # Planner uses the same simplified geometry and buffer as renderer input.
    plan = set(plan_zoom(conn, dataset, 13))
    x, y = xyz(37, 55, 13)
    for tx in range(x - 1, x + 2):
        for ty in range(y - 1, y + 2):
            if tile_inputs(conn, dataset, 13, tx, ty):
                assert (tx, ty) in plan
