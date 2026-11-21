"""Integration tests use a disposable target DB, never US_DB_* credentials."""

import os
import threading
import time
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from map_api.app import create_app
from map_sync.config import Config, SyncError
from map_sync.migrate import migrate
from map_sync.sync import cleanup, synchronize


@pytest.fixture
def databases(tmp_path):
    url = os.environ.get("TEST_MAP_DB_URL")
    if not url:
        pytest.skip("TEST_MAP_DB_URL is required for disposable PostGIS integration tests")
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    migrate(admin)
    name = "source_fixture_" + uuid4().hex
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = psycopg.conninfo.conninfo_to_dict(url)
    params["dbname"] = name
    source = psycopg.connect(**params, autocommit=True, row_factory=dict_row)
    source.execute("CREATE EXTENSION postgis")
    source.execute("CREATE SCHEMA userside3")
    source.execute(
        """
        CREATE TABLE userside3.pbl_map(code int,typemap int);
        INSERT INTO userside3.pbl_map VALUES (3,3),(9,1);
        CREATE TABLE userside3.pbl_uzel(code int,node_type_id int,parentuzel int,
            nazv text,codenumber text,location text,isplan int,mapmarkcode int,coord geometry,opis text DEFAULT '');
        INSERT INTO userside3.pbl_uzel (code,node_type_id,parentuzel,nazv,codenumber,location,isplan,mapmarkcode,coord) VALUES
            (1,4,0,'start','1','',0,0,ST_Point(55,37)),
            (2,1,0,'coupler','2','',0,0,ST_Point(55.01,37.01)),
            (3,4,1,'child','3','',0,0,NULL);
        CREATE TABLE userside3.pbl_address_building(id int,title text,coord geometry);
        CREATE TABLE userside3.pbl_optica(code int,uzelcode1 int,uzelcode2 int,
            building2_id int,cable_line_type_id int,cablecode int,owner_id int,port int,
            isplan int,ishide int,colcol text,opis text,link_length int,optical_length int);
        INSERT INTO userside3.pbl_optica VALUES (10,1,2,0,1,0,0,8,0,0,'','cable',10000,12000);
        CREATE TABLE userside3.pbl_optica_house(code int,volscode int,mapcode int,position int,x1 numeric,y1 numeric);
        INSERT INTO userside3.pbl_optica_house VALUES
            (1,10,3,2,37.006,55.006),(2,10,3,1,37.003,55.003),(3,10,9,0,1,1);
        CREATE TABLE userside3.pbl_obj_int(code int,obj1 int,usercode1 int,obj2 int,
            usercode2 int,pos int,dop1 int,additional_data_string text);
        CREATE TABLE userside3.pbl_cable_duct_content(id int,cable_duct_id int,
            cable_type int,cable_id int,position int,direction int);
        CREATE TABLE userside3.pbl_cable_duct(id int,cable_route_id int);
        CREATE TABLE userside3.pbl_cable_route(id int,coord text,object_first_type int,
            object_first_id int,object_second_type int,object_second_id int,
            first_building_id int,second_building_id int);
        CREATE TABLE userside3.pbl_base(code int,building_id int);
        CREATE TABLE userside3.pbl_node_type(id int,name text,single_name text,style text,is_layer_on_map int);
        INSERT INTO userside3.pbl_node_type VALUES
            (1,'coupler','coupler','{"template":2,"color":"ff0000"}',1),
            (4,'node','node','{"template":1}',1);
        CREATE TABLE userside3.pbl_cable_line_type(id int,name text,special_type int,
            map_color text,map_color_plan text,map_width int,is_on_map int);
        INSERT INTO userside3.pbl_cable_line_type VALUES (1,'fiber',1,'000080','848284',5,1);
        CREATE TABLE userside3.pbl_conf_layer(code int,nazv text,onmapdef int,style text);
        INSERT INTO userside3.pbl_conf_layer VALUES (1,'district',1,'{"COLOR":"ff0000"}');
        CREATE TABLE userside3.pbl_conf_layer_object(id int,layer_id int,object_uuid text);
        INSERT INTO userside3.pbl_conf_layer_object VALUES (1,1,'optica');
        CREATE TABLE userside3.pbl_mark_object(id int,object_type int,object_id int,mark_id int);
        INSERT INTO userside3.pbl_mark_object VALUES (1,2,10,1),(2,1,2,1);
        CREATE TABLE userside3.pbl_optica_mag(code int,nazv text);
        CREATE TABLE userside3.pbl_optica_mag_inc(code int,volscode int,magcode int);
        CREATE TABLE userside3.pbl_conf_map_marker(code int,nazv text);
        CREATE TABLE userside3.pbl_conf(id int,param text,valuestr text,valuememo text);
    """,
        prepare=False,
    )
    config = Config(
        params,
        url,
        source_system="fixture_" + uuid4().hex,
        batch_size=2,
        sync_report_dir=tmp_path / "reports",
    )
    try:
        yield source, admin, config
    finally:
        source.close()
        admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
        admin.close()


def quiet(_):
    pass


def active(conn, config):
    return conn.execute(
        "SELECT dataset_id FROM map_data.active_datasets WHERE source_system=%s",
        (config.source_system,),
    ).fetchone()["dataset_id"]


def test_migrate_repeat_sync_deletions_and_failed_publication(databases):
    source, target, config = databases
    assert migrate(target) == []
    first = synchronize(config, progress=quiet)
    version = first["datasetId"]
    rows = target.execute(
        """SELECT object_type,ST_AsGeoJSON(geom)::json AS geometry,
        style,properties FROM map_data.features WHERE dataset_id=%s ORDER BY source_kind,source_id""",
        (version,),
    ).fetchall()
    assert len(rows) == 4
    assert rows[0]["geometry"]["coordinates"] == [
        [37, 55],
        [37.003, 55.003],
        [37.006, 55.006],
        [37.01, 55.01],
    ]
    assert rows[0]["style"]["color"] == "#ff0000"
    assert rows[0]["properties"]["linkLengthM"] == 100
    assert (
        target.execute(
            "SELECT count(*) AS n FROM map_data.source_records WHERE run_id=%s", (first["runId"],)
        ).fetchone()["n"]
        == 0
    )
    with pytest.raises(psycopg.errors.RaiseException):
        target.execute(
            "UPDATE map_data.features SET title='changed' WHERE dataset_id=%s", (version,)
        )
    source.execute("DELETE FROM userside3.pbl_uzel WHERE code=3")
    second = synchronize(config, progress=quiet)
    assert active(target, config) == UUID(second["datasetId"])
    assert (
        target.execute(
            "SELECT count(*) AS n FROM map_data.features WHERE dataset_id=%s",
            (second["datasetId"],),
        ).fetchone()["n"]
        == 3
    )
    assert (
        target.execute(
            "SELECT count(*) AS n FROM map_data.features WHERE dataset_id=%s", (version,)
        ).fetchone()["n"]
        == 4
    )
    source.execute(
        "INSERT INTO userside3.pbl_uzel (code,node_type_id,parentuzel,nazv,codenumber,location,isplan,mapmarkcode,coord) VALUES (4,1,4,'cycle','','',0,0,NULL)"
    )
    with pytest.raises(SyncError, match="Validation failed"):
        synchronize(config, progress=quiet)
    assert str(active(target, config)) == second["datasetId"]
    issue = target.execute(
        """SELECT code FROM map_data.sync_issues i JOIN map_data.sync_runs r ON r.id=i.run_id
        WHERE r.source_system=%s ORDER BY i.id DESC LIMIT 1""",
        (config.source_system,),
    ).fetchone()
    assert issue["code"] == "parent_cycle"
    third = synchronize(config, max_errors=1, progress=quiet)
    assert third["stats"]["features"]["errors"] == 1
    assert str(active(target, config)) == third["datasetId"]


def test_skip_invalid_objects_publishes_valid_geometry_and_reports_exclusions(databases):
    source, target, config = databases
    source.execute("UPDATE userside3.pbl_uzel SET coord=ST_Point(999,999) WHERE code=2")
    result = synchronize(config, invalid_objects="skip", progress=quiet)
    assert result["stats"]["excludedObjects"] == 2  # Bad node and dependent cable.
    assert str(active(target, config)) == result["datasetId"]
    rows = target.execute(
        "SELECT source_kind,source_id,ST_IsValid(geom) AS valid FROM map_data.features WHERE dataset_id=%s",
        (result["datasetId"],),
    ).fetchall()
    assert {(r["source_kind"], r["source_id"]) for r in rows} == {("node", 1), ("node", 3)}
    assert all(r["valid"] for r in rows)
    issues = target.execute(
        "SELECT source_kind,source_id FROM map_data.sync_issues WHERE run_id=%s AND severity='error'",
        (result["runId"],),
    ).fetchall()
    assert {(r["source_kind"], r["source_id"]) for r in issues} == {("node", 2), ("cable", 10)}
    assert all(os.path.isfile(path) for path in result["reports"].values())
    source.execute("UPDATE userside3.pbl_uzel SET parentuzel=0,coord=NULL")
    with pytest.raises(SyncError, match="Empty dataset"):
        synchronize(config, invalid_objects="skip", progress=quiet)
    assert str(active(target, config)) == result["datasetId"]


def test_leases_cleanup_and_read_only_source(databases):
    source, target, config = databases
    versions = [synchronize(config, progress=quiet) for _ in range(4)]
    lease_id = uuid4()
    target.execute(
        "INSERT INTO map_data.query_leases VALUES (%s,%s,now()+interval '1 hour','{}','[]')",
        (lease_id, versions[0]["datasetId"]),
    )
    cleanup(target)
    assert target.execute(
        "SELECT id FROM map_data.datasets WHERE id=%s", (versions[0]["datasetId"],)
    ).fetchone()
    assert not target.execute(
        "SELECT id FROM map_data.datasets WHERE id=%s", (versions[1]["datasetId"],)
    ).fetchone()
    target.execute(
        "UPDATE map_data.query_leases SET expires_at=now()-interval '1 second' WHERE id=%s",
        (lease_id,),
    )
    cleanup(target)
    assert not target.execute(
        "SELECT id FROM map_data.datasets WHERE id=%s", (versions[0]["datasetId"],)
    ).fetchone()
    with config.connect_source() as readonly, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        readonly.execute("UPDATE userside3.pbl_uzel SET nazv='forbidden'")
    assert (
        source.execute("SELECT nazv FROM userside3.pbl_uzel WHERE code=1").fetchone()["nazv"]
        == "start"
    )


def test_lock_and_target_protection(databases):
    _, target, config = databases
    target.execute(
        "SELECT pg_advisory_lock(hashtextextended(%s,0))", (f"{config.source_system}:3",)
    )
    try:
        with pytest.raises(SyncError, match="already running"):
            synchronize(config, progress=quiet)
    finally:
        target.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (f"{config.source_system}:3",)
        )
    source_url = psycopg.conninfo.make_conninfo(**config.source)
    with pytest.raises(SyncError, match="refusing writes"):
        Config(config.source, source_url).connect_target()


def test_source_snapshot_and_partial_dataset_visibility(databases):
    source, target, config = databases
    first = synchronize(config, progress=quiet)
    changed = False

    def during_import(event):
        nonlocal changed
        assert str(active(target, config)) == first["datasetId"]
        if event["stage"] == "extract" and event["kind"] == "map":
            source.execute("DELETE FROM userside3.pbl_uzel WHERE code=3")
            changed = True

    # The published event follows the atomic switch, so check only building stages.
    second = synchronize(
        config,
        progress=lambda event: during_import(event) if event["stage"] != "published" else None,
    )
    assert changed
    assert second["stats"]["features"]["node"] == 2
    third = synchronize(config, progress=quiet)
    assert third["stats"]["features"]["node"] == 1


def test_interrupted_job_and_missing_source_table(databases):
    source, target, config = databases
    first = synchronize(config, progress=quiet)
    abandoned_run, abandoned_dataset = uuid4(), uuid4()
    target.execute(
        """INSERT INTO map_data.sync_runs
        (id,source_system,map_id,status,rules_version) VALUES (%s,%s,3,'running','test')""",
        (abandoned_run, config.source_system),
    )
    target.execute(
        """INSERT INTO map_data.datasets
        (id,run_id,source_system,map_id,status) VALUES (%s,%s,%s,3,'building')""",
        (abandoned_dataset, abandoned_run, config.source_system),
    )
    source.execute("DROP TABLE userside3.pbl_node_type")
    with pytest.raises(psycopg.errors.UndefinedTable):
        synchronize(config, progress=quiet)
    assert str(active(target, config)) == first["datasetId"]
    assert (
        target.execute(
            "SELECT status FROM map_data.sync_runs WHERE id=%s", (abandoned_run,)
        ).fetchone()["status"]
        == "interrupted"
    )
    assert (
        target.execute(
            "SELECT status FROM map_data.datasets WHERE id=%s", (abandoned_dataset,)
        ).fetchone()["status"]
        == "failed"
    )


def test_publication_waits_for_inflight_feature_write(databases):
    _, target, config = databases
    run_id, dataset_id = uuid4(), uuid4()
    target.execute(
        """INSERT INTO map_data.sync_runs
        (id,source_system,map_id,status,rules_version) VALUES (%s,%s,3,'running','test')""",
        (run_id, config.source_system),
    )
    target.execute(
        """INSERT INTO map_data.datasets
        (id,run_id,source_system,map_id,status) VALUES (%s,%s,%s,3,'building')""",
        (dataset_id, run_id, config.source_system),
    )
    insert = """INSERT INTO map_data.features
        (dataset_id,source_kind,source_id,object_type,geom,min_zoom,vertex_count)
        VALUES (%s,'node',%s,'node',ST_SetSRID(ST_Point(37,55),4326),15,1)"""
    target.execute("SET lock_timeout='200ms'")
    try:
        with config.connect_target() as writer, writer.transaction():
            writer.execute(insert, (dataset_id, 1))
            with pytest.raises(psycopg.errors.LockNotAvailable):
                target.execute(
                    "UPDATE map_data.datasets SET status='ready',published_at=now() WHERE id=%s",
                    (dataset_id,),
                )
        target.execute(
            "UPDATE map_data.datasets SET status='ready',published_at=now() WHERE id=%s",
            (dataset_id,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            target.execute(insert, (dataset_id, 2))
        assert (
            target.execute(
                "SELECT count(*) AS n FROM map_data.features WHERE dataset_id=%s", (dataset_id,)
            ).fetchone()["n"]
            == 1
        )
    finally:
        target.execute("RESET lock_timeout")


def test_point_zoom_override_changes_only_nodes_and_couplers(databases):
    _, _, config = databases
    synchronize(config, progress=quiet)
    app = create_app(
        settings={"database_url": config.target_url, "source_system": config.source_system}
    )
    request = {
        "mapId": 3,
        "bbox": [36, 54, 38, 56],
        "zoom": 12,
        "objectTypes": ["node", "coupler", "cable_line"],
    }
    with TestClient(app) as client:

        def counts(**changes):
            response = client.post("/api/map/objects/preload", json={**request, **changes})
            assert response.status_code == 200
            return response.json()["typeCounts"]

        assert counts() == {"cable_line": 1}
        assert counts(pointMinZoom=12) == {"cable_line": 1, "node": 2, "coupler": 1}
        assert counts(pointMinZoom=13) == {"cable_line": 1}
        assert counts(zoom=0, pointMinZoom=0) == {"node": 2, "coupler": 1}
        assert counts(pointMinZoom=12, layerIds=[1]) == {"cable_line": 1, "coupler": 1}
        assert (
            client.post(
                "/api/map/objects/preload", json={**request, "pointMinZoom": -1}
            ).status_code
            == 422
        )


def test_map_api_pins_spatial_selection_and_returns_batches(databases):
    source, target, config = databases
    source.execute(
        "INSERT INTO userside3.pbl_cable_line_type VALUES (3,'UTP',0,'ed008c','ed008c',1,1)"
    )
    source.execute(
        """INSERT INTO userside3.pbl_optica VALUES
        (12,1,2,0,3,0,0,4,0,0,'','other cable type',10000,12000)"""
    )
    source.execute(
        """INSERT INTO userside3.pbl_optica_house VALUES
        (4,12,3,2,37.006,55.006),(5,12,3,1,37.003,55.003)"""
    )
    source.execute(
        """INSERT INTO userside3.pbl_uzel (code,node_type_id,parentuzel,nazv,codenumber,location,isplan,mapmarkcode,coord) VALUES
        (100,4,0,'east dateline','100','',1,0,ST_Point(0,179.5)),
        (101,4,0,'west dateline','101','',1,0,ST_Point(0,-179.5))"""
    )
    source.execute(
        """INSERT INTO userside3.pbl_optica VALUES
        (11,1,2,0,1,0,0,8,0,1,'','hidden cable',10000,12000)"""
    )
    published = synchronize(config, progress=quiet)
    app = create_app(
        settings={"database_url": config.target_url, "source_system": config.source_system}
    )
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").json()["status"] == "ready"
        layer_response = client.get("/api/map/layers?mapId=3")
        assert layer_response.status_code == 200
        assert layer_response.json()["datasetVersion"] == published["datasetId"]
        assert (
            next(
                item["count"]
                for item in layer_response.json()["objectTypes"]
                if item["object_type"] == "cable_line"
            )
            == 1
        )
        assert client.get("/api/map/layers?mapId=999").status_code == 503
        imported_type_3 = target.execute(
            """SELECT count(*) AS n FROM map_data.features
            WHERE dataset_id=%s AND source_kind='cable' AND properties->>'cableTypeId'='3'""",
            (published["datasetId"],),
        ).fetchone()["n"]
        assert imported_type_3 == 1

        # Both cable endpoints lie outside this tiny bbox; the complete line crosses it.
        request = {
            "mapId": 3,
            "bbox": [37.004, 55.004, 37.005, 55.005],
            "zoom": 18,
            "objectTypes": ["cable_line", "node", "coupler"],
            "layerIds": [1],
        }
        manifest_response = client.post("/api/map/objects/preload", json=request)
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        assert manifest["datasetVersion"] == published["datasetId"]
        assert manifest["totalObjects"] == 1
        assert manifest["typeCounts"] == {"cable_line": 1}
        assert manifest["batches"] == [
            {"index": 0, "objectCount": 1, "vertexCount": 4, "oversized": False}
        ]
        cable_only = client.post(
            "/api/map/objects/preload",
            json={**request, "objectTypes": ["cable_line"], "layerIds": []},
        )
        assert cable_only.status_code == 200
        assert cable_only.json()["totalObjects"] == 1
        assert cable_only.json()["typeCounts"] == {"cable_line": 1}
        unmatched_layer = client.post(
            "/api/map/objects/preload",
            json={**request, "layerIds": [999]},
        )
        assert unmatched_layer.status_code == 200
        assert unmatched_layer.json()["totalObjects"] == 0

        extended_app = create_app(
            settings={
                "database_url": config.target_url,
                "source_system": config.source_system,
                "visible_cable_type_ids": [1, 3],
            }
        )
        with TestClient(extended_app) as extended_client:
            extended_preload = extended_client.post(
                "/api/map/objects/preload",
                json={**request, "objectTypes": ["cable_line"], "layerIds": []},
            )
            assert extended_preload.status_code == 200
            assert extended_preload.json()["totalObjects"] == 2

        dateline_request = {
            "mapId": 3,
            "bbox": [170, -1, -170, 1],
            "zoom": 15,
            "objectTypes": ["node"],
        }
        too_early = client.post("/api/map/objects/preload", json={**dateline_request, "zoom": 14})
        assert too_early.status_code == 200
        assert too_early.json()["totalObjects"] == 0
        dateline = client.post("/api/map/objects/preload", json=dateline_request)
        assert dateline.status_code == 200
        assert dateline.json()["totalObjects"] == 2
        assert [batch["objectCount"] for batch in dateline.json()["batches"]] == [2]
        coupler = client.post(
            "/api/map/objects/preload",
            json={
                "mapId": 3,
                "bbox": [37.009, 55.009, 37.011, 55.011],
                "zoom": 15,
                "objectTypes": ["coupler"],
            },
        )
        assert coupler.status_code == 200
        assert coupler.json()["typeCounts"] == {"coupler": 1}

        empty = client.post(
            "/api/map/objects/preload",
            json={**request, "objectTypes": [], "layerIds": []},
        )
        assert empty.status_code == 200
        assert empty.json()["totalObjects"] == 0
        assert empty.json()["batches"] == []

        # Cleanup cannot remove a lease while another worker holds the same
        # FOR SHARE lock used by batch reads.
        empty_query_id = empty.json()["queryId"]
        target.execute(
            "UPDATE map_data.query_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (empty_query_id,),
        )
        cleanup_finished = threading.Event()
        cleanup_errors = []

        def clean_expired_leases():
            try:
                with config.connect_target() as cleanup_conn:
                    cleanup(cleanup_conn)
            except psycopg.Error as error:
                cleanup_errors.append(error)
            finally:
                cleanup_finished.set()

        with config.connect_target() as lease_reader, lease_reader.transaction():
            lease_reader.execute(
                "SELECT id FROM map_data.query_leases WHERE id=%s FOR SHARE",
                (empty_query_id,),
            )
            cleaner = threading.Thread(target=clean_expired_leases)
            cleaner.start()
            assert not cleanup_finished.wait(0.2)
        cleaner.join(timeout=5)
        assert cleanup_finished.is_set()
        assert not cleanup_errors
        assert (
            target.execute(
                "SELECT id FROM map_data.query_leases WHERE id=%s", (empty_query_id,)
            ).fetchone()
            is None
        )

        # Publishing a newer snapshot does not change an already pinned query.
        before_publish = threading.Event()
        allow_publish = threading.Event()
        publish_results = []
        publish_errors = []

        def gate_publication(event):
            if event.get("stage") == "build" and event.get("kind") == "cable":
                before_publish.set()
                if not allow_publish.wait(10):
                    raise RuntimeError("Test did not release publication gate")

        def synchronize_replacement():
            try:
                publish_results.append(synchronize(config, progress=gate_publication))
            except (psycopg.Error, SyncError, RuntimeError, ValueError) as error:
                publish_errors.append(error)

        publisher = threading.Thread(target=synchronize_replacement)
        try:
            with config.connect_target() as pin_conn, pin_conn.transaction():
                pinned = pin_conn.execute(
                    "SELECT map_data.api_pin_active_dataset(%s,%s) AS dataset_id",
                    (config.source_system, 3),
                ).fetchone()["dataset_id"]
                assert str(pinned) == manifest["datasetVersion"]
                publisher.start()
                assert before_publish.wait(10)
                allow_publish.set()
                deadline = time.monotonic() + 5
                waiting = None
                while time.monotonic() < deadline:
                    waiting = target.execute(
                        """SELECT wait_event_type FROM pg_stat_activity
                        WHERE state='active' AND wait_event_type='Lock'
                          AND query LIKE 'INSERT INTO map_data.active_datasets%'
                        LIMIT 1"""
                    ).fetchone()
                    if waiting:
                        break
                    threading.Event().wait(0.01)
                assert waiting
                active_during_pin = target.execute(
                    "SELECT dataset_id FROM map_data.active_datasets WHERE source_system=%s AND map_id=3",
                    (config.source_system,),
                ).fetchone()["dataset_id"]
                assert str(active_during_pin) == manifest["datasetVersion"]
                assert publisher.is_alive()
        finally:
            allow_publish.set()
        publisher.join(timeout=10)
        assert not publisher.is_alive()
        assert not publish_errors
        replacement = publish_results[0]
        assert replacement["datasetId"] != manifest["datasetVersion"]
        assert (
            client.get("/api/map/layers?mapId=3").json()["datasetVersion"]
            == replacement["datasetId"]
        )

        body = {"queryId": manifest["queryId"], "batchIndex": 0}
        missing_batch = client.post(
            "/api/map/objects/batch",
            json={"queryId": manifest["queryId"], "batchIndex": 99},
        )
        assert missing_batch.status_code == 404
        assert missing_batch.json()["error"]["code"] == "BATCH_NOT_FOUND"
        second_worker = create_app(
            settings={"database_url": config.target_url, "source_system": config.source_system}
        )
        with TestClient(second_worker) as worker_client:
            first = worker_client.post("/api/map/objects/batch", json=body)
        retry = client.post("/api/map/objects/batch", json=body)
        assert first.status_code == retry.status_code == 200
        assert first.json()["datasetVersion"] == manifest["datasetVersion"]
        feature = first.json()["featureCollection"]["features"][0]
        assert feature["id"] == "cable:10"
        assert feature["geometry"]["coordinates"] == [
            [37, 55],
            [37.003, 55.003],
            [37.006, 55.006],
            [37.01, 55.01],
        ]
        assert first.json() == retry.json()

        detail = client.get(f"/api/map/objects/cable/10?queryId={manifest['queryId']}")
        assert detail.status_code == 200
        assert detail.json()["layers"][0]["id"] == 1
        assert {link["role"] for link in detail.json()["links"]} == {"start", "end"}
        assert detail.json()["cableEndpoints"] == ["start", "coupler"]
        assert (
            client.get(f"/api/map/objects/cable/999?queryId={manifest['queryId']}").status_code
            == 404
        )
        before_failed_preloads = target.execute(
            "SELECT count(*) AS n FROM map_data.query_leases WHERE dataset_id=%s",
            (published["datasetId"],),
        ).fetchone()["n"]
        selection_limit_app = create_app(
            settings={
                "database_url": config.target_url,
                "source_system": config.source_system,
                "max_objects": 1,
            }
        )
        with TestClient(selection_limit_app) as limit_client:
            over_limit = limit_client.post(
                "/api/map/objects/preload",
                json={
                    "mapId": 3,
                    "bbox": [-180, -90, 180, 90],
                    "zoom": 24,
                    "objectTypes": ["cable_line", "node", "coupler"],
                },
            )
        assert over_limit.status_code == 413
        assert over_limit.json()["error"]["code"] == "QUERY_LIMIT_EXCEEDED"
        hard_limit_app = create_app(
            settings={
                "database_url": config.target_url,
                "source_system": config.source_system,
                "hard_vertices": 1,
            }
        )
        with TestClient(hard_limit_app) as hard_limit_client:
            oversized_object = hard_limit_client.post(
                "/api/map/objects/preload",
                json={**request, "objectTypes": ["cable_line"], "layerIds": []},
            )
        assert oversized_object.status_code == 413
        assert oversized_object.json()["error"]["code"] == "OBJECT_TOO_LARGE"
        timeout_app = create_app(
            settings={
                "database_url": config.target_url,
                "source_system": config.source_system,
                "preload_timeout_seconds": 0,
            }
        )
        with TestClient(timeout_app) as timeout_client:
            timed_out = timeout_client.post(
                "/api/map/objects/preload",
                json={
                    "mapId": 3,
                    "bbox": [-180, -90, 180, 90],
                    "zoom": 24,
                    "objectTypes": ["cable_line"],
                },
            )
        assert timed_out.status_code == 503
        assert timed_out.json()["error"]["code"] == "PRELOAD_TIMEOUT"
        assert (
            target.execute(
                "SELECT count(*) AS n FROM map_data.query_leases WHERE dataset_id=%s",
                (published["datasetId"],),
            ).fetchone()["n"]
            == before_failed_preloads
        )
        waiting_response = {}

        def request_expiring_batch():
            waiting_app = create_app(
                settings={"database_url": config.target_url, "source_system": config.source_system}
            )
            with TestClient(waiting_app) as waiting_client:
                waiting_response["response"] = waiting_client.post(
                    "/api/map/objects/batch", json=body
                )

        batch_thread = threading.Thread(target=request_expiring_batch)
        with config.connect_target() as lease_updater, lease_updater.transaction():
            lease_updater.execute(
                "SELECT id FROM map_data.query_leases WHERE id=%s FOR UPDATE",
                (manifest["queryId"],),
            )
            lease_updater.execute(
                "UPDATE map_data.query_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (manifest["queryId"],),
            )
            batch_thread.start()
            deadline = time.monotonic() + 5
            waiting = None
            while time.monotonic() < deadline:
                waiting = target.execute(
                    """SELECT wait_event_type FROM pg_stat_activity
                    WHERE state='active' AND wait_event_type='Lock'
                      AND query LIKE 'SELECT id,dataset_id,expires_at,query FROM map_data.query_leases%'
                    LIMIT 1"""
                ).fetchone()
                if waiting:
                    break
                threading.Event().wait(0.01)
            assert waiting
        batch_thread.join(timeout=5)
        assert not batch_thread.is_alive()
        expired_response = waiting_response["response"]
        assert expired_response.status_code == 410
        assert expired_response.json()["error"]["code"] == "QUERY_EXPIRED"


def test_cable_group_is_one_complete_spatial_object(databases):
    source, target, config = databases
    source.execute("INSERT INTO userside3.pbl_optica_mag VALUES (50,'whole route'),(51,'empty')")
    # A disconnected segment of a different, hidden type must still be included.
    source.execute(
        "INSERT INTO userside3.pbl_cable_line_type VALUES (3,'other',0,'ffffff','aaaaaa',2,0)"
    )
    source.execute("""INSERT INTO userside3.pbl_uzel (code,node_type_id,parentuzel,nazv,codenumber,location,isplan,mapmarkcode,coord) VALUES
        (20,8,0,'pole','','',0,0,ST_Point(56,38))""")
    source.execute(
        "INSERT INTO userside3.pbl_address_building VALUES (21,'far building',ST_Point(56.001,38.001))"
    )
    source.execute("""INSERT INTO userside3.pbl_optica VALUES
        (11,20,0,21,3,0,0,4,0,1,'','far piece',20000,24000),
        (12,1,2,0,1,0,0,8,0,0,'','standalone',30000,36000)""")
    source.execute("INSERT INTO userside3.pbl_optica_mag_inc VALUES (1,10,50),(2,11,50),(3,11,50)")
    first = synchronize(config, progress=quiet)
    rows = target.execute(
        """SELECT source_kind,source_id,ST_AsGeoJSON(geom)::json AS geometry,
        vertex_count,properties,length_m,min_zoom FROM map_data.features
        WHERE dataset_id=%s AND object_type='cable_line' ORDER BY source_kind""",
        (first["datasetId"],),
    ).fetchall()
    assert [(r["source_kind"], r["source_id"]) for r in rows] == [
        ("cable", 12),
        ("cable_group", 50),
    ]
    group = rows[1]
    assert group["geometry"]["type"] == "MultiLineString"
    assert group["geometry"]["coordinates"] == [
        [[37, 55], [37.003, 55.003], [37.006, 55.006], [37.01, 55.01]],
        [[38, 56], [38.001, 56.001]],
    ]
    assert group["vertex_count"] == 6
    assert group["properties"]["memberIds"] == [10, 11]
    assert group["properties"]["linkLengthM"] == 300
    assert group["properties"]["cableTypeIds"] == ["1"]
    assert group["min_zoom"] == 1
    app = create_app(
        settings={
            "database_url": config.target_url,
            "source_system": config.source_system,
            "batch_vertices": 5,
        }
    )
    with TestClient(app) as client:
        catalog = client.get("/api/map/layers?mapId=3").json()
        assert (
            next(t["count"] for t in catalog["objectTypes"] if t["object_type"] == "cable_line")
            == 2
        )
        # Only the otherwise-hidden second piece crosses this viewport.
        request = {
            "mapId": 3,
            "bbox": [38.0004, 56.0004, 38.0006, 56.0006],
            "zoom": 1,
            "objectTypes": ["cable_line"],
            "layerIds": [1],
        }
        manifest = client.post("/api/map/objects/preload", json=request).json()
        assert manifest["totalObjects"] == 1
        assert manifest["batches"] == [
            {"index": 0, "objectCount": 1, "vertexCount": 6, "oversized": True}
        ]
        batch = client.post(
            "/api/map/objects/batch", json={"queryId": manifest["queryId"], "batchIndex": 0}
        ).json()
        assert batch["objectCount"] == 1
        feature = batch["featureCollection"]["features"][0]
        assert feature["id"] == "cable_group:50"
        assert feature["geometry"] == group["geometry"]
        details = client.get(
            "/api/map/objects/cable_group/50", params={"queryId": manifest["queryId"]}
        ).json()
        assert details["properties"]["memberCount"] == 2
        assert len(details["links"]) == 2
        assert details["cableEndpoints"] == ["start", "coupler", "pole", "far building"]
        assert details["layers"][0]["id"] == 1
        # The gap between pieces is not filled by a fictitious connecting line.
        gap = client.post(
            "/api/map/objects/preload", json={**request, "bbox": [37.49, 55.49, 37.51, 55.51]}
        ).json()
        assert gap["totalObjects"] == 0
        assert (
            client.post("/api/map/objects/preload", json={**request, "layerIds": [999]}).json()[
                "totalObjects"
            ]
            == 0
        )
    # A broken terminal member is dropped with a warning; publication still succeeds.
    source.execute("UPDATE userside3.pbl_optica SET building2_id=999 WHERE code=11")
    partial = synchronize(config, progress=quiet)
    assert str(active(target, config)) == partial["datasetId"]
    partial_group = target.execute(
        "SELECT properties FROM map_data.features WHERE dataset_id=%s AND source_kind='cable_group'",
        (partial["datasetId"],),
    ).fetchone()["properties"]
    assert partial_group["droppedMemberIds"] == [11]
    assert partial["stats"]["features"].get("errors", 0) == 0
    assert partial["stats"]["features"]["warnings"] == 1
    # Removal from the source membership changes the next version, not the pinned one.
    source.execute("DELETE FROM userside3.pbl_optica_mag_inc WHERE volscode=11")
    source.execute("DELETE FROM userside3.pbl_optica WHERE code=11")
    second = synchronize(config, progress=quiet)
    assert target.execute(
        "SELECT properties FROM map_data.features WHERE dataset_id=%s AND source_kind='cable_group'",
        (second["datasetId"],),
    ).fetchone()["properties"]["memberIds"] == [10]
    assert target.execute(
        "SELECT properties FROM map_data.features WHERE dataset_id=%s AND source_kind='cable_group'",
        (first["datasetId"],),
    ).fetchone()["properties"]["memberIds"] == [10, 11]


@pytest.mark.parametrize("policy", ["bridge", "drop"])
def test_group_geometry_errors_do_not_block_publication(databases, policy):
    from dataclasses import replace

    source, target, config = databases
    config = replace(config, cable_group_error_policy=policy)
    source.execute("INSERT INTO userside3.pbl_optica_mag VALUES (50,'repair route')")
    source.execute("""INSERT INTO userside3.pbl_uzel (code,node_type_id,parentuzel,nazv,codenumber,location,isplan,mapmarkcode,coord) VALUES
        (20,8,0,'unknown pole 1','','',0,0,NULL),
        (21,8,0,'unknown pole 2','','',0,0,NULL),
        (22,8,0,'known pole','','',0,0,ST_Point(55.03,37.03))""")
    source.execute("""INSERT INTO userside3.pbl_optica VALUES
        (11,2,20,0,1,0,0,8,0,0,'','broken 1',100,100),
        (12,21,20,0,1,0,0,8,0,0,'','broken reversed',100,100),
        (13,21,22,0,1,0,0,8,0,0,'','broken 3',100,100),
        (14,22,1,0,1,0,0,8,0,0,'','good',100,100)""")
    source.execute(
        "INSERT INTO userside3.pbl_optica_mag_inc VALUES (1,10,50),(2,11,50),(3,12,50),(4,13,50),(5,14,50)"
    )
    result = synchronize(config, progress=quiet)
    assert result["stats"]["features"].get("errors", 0) == 0
    assert result["stats"]["features"]["warnings"] == 3
    group = target.execute(
        "SELECT properties,ST_AsGeoJSON(geom)::json AS geometry,vertex_count FROM map_data.features WHERE dataset_id=%s AND source_kind='cable_group'",
        (result["datasetId"],),
    ).fetchone()
    props = group["properties"]
    assert props["errorPolicy"] == policy
    assert props["memberIds"] == [10, 11, 12, 13, 14]
    assert props["reconstructedMemberIds"] == ([11, 12, 13] if policy == "bridge" else [])
    assert props["droppedMemberIds"] == ([] if policy == "bridge" else [11, 12, 13])
    assert len(group["geometry"]["coordinates"]) == (3 if policy == "bridge" else 2)
    assert group["vertex_count"] == (8 if policy == "bridge" else 6)
    if policy == "bridge":
        assert group["geometry"]["coordinates"][-1] == [[37.01, 55.01], [37.03, 55.03]]
    severities = target.execute(
        "SELECT DISTINCT severity FROM map_data.sync_issues WHERE run_id=%s", (result["runId"],)
    ).fetchall()
    assert severities == [{"severity": "warning"}]
    import json
    from pathlib import Path

    report = json.loads(Path(result["reports"]["json"]).read_text())
    assert report["status"] == "succeeded"
    assert {row["id"] for row in report["objects"]} == {11, 12, 13}
    assert all(
        row["type"] == "cable" and row["group_id"] == 50 and row["severity"] == "warning"
        for row in report["objects"]
    )
    assert all(row["description"].startswith("broken") for row in report["objects"])
    assert (
        target.execute(
            "SELECT config FROM map_data.datasets WHERE id=%s", (result["datasetId"],)
        ).fetchone()["config"]["cableGroupErrorPolicy"]
        == policy
    )
    with TestClient(
        create_app(
            settings={"database_url": config.target_url, "source_system": config.source_system}
        )
    ) as client:
        manifest = client.post(
            "/api/map/objects/preload",
            json={
                "mapId": 3,
                "bbox": [37, 55, 37.04, 55.04],
                "zoom": 18,
                "objectTypes": ["cable_line"],
                "layerIds": [],
            },
        ).json()
        assert manifest["totalObjects"] == 1
        feature = client.post(
            "/api/map/objects/batch", json={"queryId": manifest["queryId"], "batchIndex": 0}
        ).json()["featureCollection"]["features"][0]
        assert feature["geometry"] == group["geometry"]


def test_issue_report_contains_object_text_on_failed_sync_and_after_cleanup(databases):
    import json
    from pathlib import Path

    from map_sync.issue_report import save_issue_report

    source, target, config = databases
    good = synchronize(config, progress=quiet)
    assert json.loads(Path(good["reports"]["json"]).read_text())["objects"] == []
    source.execute("""INSERT INTO userside3.pbl_uzel
        (code,node_type_id,parentuzel,nazv,opis) VALUES
        (99,1,0,'Муфта с ошибкой','Описание | строка 1\nстрока 2')""")
    source.execute(
        "UPDATE userside3.pbl_optica SET uzelcode2=99,opis='Кабель до муфты' WHERE code=10"
    )
    events = []
    with pytest.raises(SyncError, match="Validation failed"):
        synchronize(config, progress=events.append)
    event = next(e for e in events if e["stage"] == "issue_report")
    document = json.loads(Path(event["reports"]["json"]).read_text())
    assert document["status"] == "failed"
    assert document["issue_count"] == 2
    by_type = {row["type"]: row for row in document["objects"]}
    assert by_type["node"]["id"] == 99
    assert by_type["node"]["name"] == "Муфта с ошибкой"
    assert by_type["node"]["description"] == "Описание | строка 1\nстрока 2"
    assert by_type["node"]["code"] == "missing_coordinate"
    assert by_type["node"]["reason"]
    assert by_type["cable"]["id"] == 10
    assert by_type["cable"]["name"] == "start → Муфта с ошибкой"
    assert by_type["cable"]["description"] == "Кабель до муфты"
    markdown = Path(event["reports"]["md"]).read_text()
    assert "Описание &#124; строка 1<br>строка 2" in markdown
    assert str(active(target, config)) == good["datasetId"]
    # Names survive deletion of source staging and the failed dataset.
    target.execute("DELETE FROM map_data.datasets WHERE run_id=%s", (event["runId"],))
    target.execute("DELETE FROM map_data.source_records WHERE run_id=%s", (event["runId"],))
    restored = save_issue_report(target, event["runId"], config.sync_report_dir / "restored")
    assert json.loads(Path(restored["json"]).read_text())["objects"] == document["objects"]


def test_report_write_failure_is_explicit_and_does_not_mislabel_published_dataset(databases):
    from dataclasses import replace

    _source, target, config = databases
    config.sync_report_dir.write_text("not a directory")
    with pytest.raises(
        SyncError, match="Dataset published, but the issue report could not be saved"
    ):
        synchronize(replace(config, sync_report_dir=config.sync_report_dir), progress=quiet)
    assert (
        target.execute(
            "SELECT status FROM map_data.datasets WHERE id=%s", (active(target, config),)
        ).fetchone()["status"]
        == "ready"
    )
    assert (
        target.execute(
            "SELECT status FROM map_data.sync_runs WHERE source_system=%s ORDER BY started_at DESC LIMIT 1",
            (config.source_system,),
        ).fetchone()["status"]
        == "succeeded"
    )
