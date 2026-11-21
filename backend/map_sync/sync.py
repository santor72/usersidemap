import json
from collections import Counter
from uuid import uuid4

from psycopg.types.json import Jsonb

from .cable_groups import assemble_group, endpoints, vertex_count
from .config import SyncError
from .geometry import GeometryBuilder, ObjectError, legacy_length, min_zoom, parse_json
from .issue_report import object_text, save_issue_report
from .locks import lock_dataset_lifecycle
from .source import copy_snapshot
from .store import SnapshotStore
from .styles import Styles

RULES_VERSION = "userside-geographic-v3-cable-group-repair"


def report(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def add_issue(conn, run_id, kind, source_id, error, severity="error"):
    object_kind = getattr(error, "object_kind", kind)
    object_id = getattr(error, "object_id", source_id)
    title, description = object_text(conn, run_id, object_kind, object_id)
    conn.execute(
        """INSERT INTO map_data.sync_issues
        (run_id,source_kind,source_id,severity,code,detail,
         object_kind,object_id,object_title,object_description)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            run_id,
            kind,
            source_id,
            severity,
            error.code,
            error.detail,
            object_kind,
            object_id,
            title,
            description,
        ),
    )


def add_feature(
    conn,
    dataset_id,
    kind,
    source_id,
    object_type,
    title,
    geom,
    zoom,
    hidden,
    planned,
    style,
    properties,
    length=None,
):
    conn.execute(
        """INSERT INTO map_data.features
        (dataset_id,source_kind,source_id,object_type,title,geom,min_zoom,hidden,planned,
         style,properties,length_m,vertex_count)
        VALUES (%s,%s,%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),%s,%s,%s,%s,%s,%s,%s)""",
        (
            dataset_id,
            kind,
            source_id,
            object_type,
            title or "",
            json.dumps(geom),
            zoom,
            hidden,
            planned,
            Jsonb(style),
            Jsonb(properties),
            length,
            vertex_count(geom),
        ),
    )


def add_memberships(conn, dataset_id, store, kind, source_id):
    for layer_id in set(store.marks(kind, source_id)):
        conn.execute(
            "INSERT INTO map_data.feature_layers VALUES (%s,%s,%s,%s)",
            (dataset_id, kind, source_id, layer_id),
        )


def endpoint_titles(store, references):
    titles = {}
    for kind, source_id in set(references):
        if source_id:
            row = store.get(kind, source_id) or {}
            title = row.get("nazv" if kind == "node" else "title")
            if title and title.strip():
                titles[f"{kind}:{source_id}"] = title.strip()
    return titles


def build_layers(conn, dataset_id, store, batch_size):
    for batch in store.batches("layer", batch_size):
        with conn.transaction():
            for row in batch:
                style = parse_json(row.get("style"), default={})
                objects = [r["object_uuid"] for r in store.children("layer_object", row["code"])]
                conn.execute(
                    "INSERT INTO map_data.layers VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        dataset_id,
                        row["code"],
                        row.get("nazv") or "",
                        row.get("onmapdef") == 1,
                        Jsonb(style),
                        Jsonb(objects),
                    ),
                )


def build_features(conn, dataset_id, run_id, config, progress=report):
    store = SnapshotStore(conn, run_id)
    geometry = GeometryBuilder(store, config.axes)
    styles = Styles(store)
    build_layers(conn, dataset_id, store, config.batch_size)
    counts = Counter()
    for kind in ("node", "cable"):
        for batch in store.batches(kind, config.batch_size):
            with conn.transaction():
                for row in batch:
                    source_id = row["code"]
                    if kind == "node" and row["node_type_id"] not in (1, 4):
                        continue
                    try:
                        with conn.transaction():
                            if kind == "cable" and store.children("mag", source_id):
                                for membership in store.children("mag", source_id):
                                    if not store.get("cable_group", membership["magcode"]):
                                        raise ObjectError(
                                            "missing_cable_group",
                                            "Cable references an absent group",
                                        )
                                # Groups are published below; do not draw member cables twice.
                                continue
                            if kind == "node":
                                object_type = "coupler" if row["node_type_id"] == 1 else "node"
                                coord = geometry.node(source_id)
                                add_feature(
                                    conn,
                                    dataset_id,
                                    kind,
                                    source_id,
                                    object_type,
                                    row.get("nazv"),
                                    {"type": "Point", "coordinates": coord},
                                    15,
                                    False,
                                    row.get("isplan") == 1,
                                    styles.node(row),
                                    {
                                        "nodeTypeId": row["node_type_id"],
                                        "parentId": row.get("parentuzel"),
                                        "number": row.get("codenumber"),
                                        "location": row.get("location"),
                                    },
                                )
                            else:
                                object_type = "cable_line"
                                coords, route_source = geometry.cable(row)
                                length = legacy_length(coords)
                                cable_type = store.get("cable_type", row["cable_line_type_id"])
                                if not cable_type:
                                    raise ObjectError("missing_cable_type", "Cable type is absent")
                                cable_ends = endpoints(row)
                                add_feature(
                                    conn,
                                    dataset_id,
                                    kind,
                                    source_id,
                                    object_type,
                                    row.get("opis"),
                                    {"type": "LineString", "coordinates": coords},
                                    min_zoom(length),
                                    row.get("ishide") == 1 or cable_type.get("is_on_map") != 1,
                                    row.get("isplan") == 1,
                                    styles.cable(row),
                                    {
                                        "cableTypeId": row["cable_line_type_id"],
                                        "isBackbone": bool(store.children("mag", row["code"])),
                                        "specialType": cable_type["special_type"],
                                        "routeSource": route_source,
                                        "coreCount": row.get("port"),
                                        "catalogId": row.get("cablecode"),
                                        "ownerId": row.get("owner_id"),
                                        "linkLengthM": (row.get("link_length") or 0) / 100,
                                        "opticalLengthM": (row.get("optical_length") or 0) / 100,
                                        "endpointTitles": endpoint_titles(store, cable_ends),
                                    },
                                    length,
                                )
                                for role, target in zip(("start", "end"), cable_ends):
                                    conn.execute(
                                        "INSERT INTO map_data.feature_links VALUES (%s,%s,%s,%s,%s,%s)",
                                        (dataset_id, kind, source_id, role, *target),
                                    )
                            add_memberships(conn, dataset_id, store, kind, source_id)
                        counts[object_type] += 1
                    except ObjectError as error:
                        add_issue(conn, run_id, kind, source_id, error)
                        counts["errors"] += 1
            progress({"stage": "build", "kind": kind, "counts": dict(counts)})
    for batch in store.batches("cable_group", config.batch_size):
        with conn.transaction():
            for row in batch:
                try:
                    with conn.transaction():

                        def group_warning(error, group_id=row["code"]):
                            add_issue(conn, run_id, "cable_group", group_id, error, "warning")
                            counts["warnings"] += 1

                        group = assemble_group(
                            store,
                            geometry,
                            styles,
                            row,
                            error_policy=config.cable_group_error_policy,
                            on_issue=group_warning,
                        )
                        if group is None:
                            continue
                        group["properties"]["endpointTitles"] = endpoint_titles(
                            store,
                            (
                                (end["sourceKind"], end["sourceId"])
                                for member in group["properties"]["members"]
                                if member["status"] != "dropped"
                                for end in (member["start"], member["end"])
                            ),
                        )
                        add_feature(
                            conn,
                            dataset_id,
                            "cable_group",
                            row["code"],
                            "cable_line",
                            row.get("nazv"),
                            group["geometry"],
                            group["zoom"],
                            group["hidden"],
                            group["planned"],
                            group["style"],
                            group["properties"],
                            group["length"],
                        )
                        for member_id in group["properties"]["memberIds"]:
                            conn.execute(
                                "INSERT INTO map_data.feature_links VALUES (%s,%s,%s,%s,%s,%s)",
                                (
                                    dataset_id,
                                    "cable_group",
                                    row["code"],
                                    f"member:{member_id}",
                                    "cable",
                                    member_id,
                                ),
                            )
                        for layer_id in group["layers"]:
                            conn.execute(
                                "INSERT INTO map_data.feature_layers VALUES (%s,%s,%s,%s)",
                                (dataset_id, "cable_group", row["code"], layer_id),
                            )
                    counts["cable_line"] += 1
                    counts["cable_groups"] += 1
                except ObjectError as error:
                    add_issue(conn, run_id, "cable_group", row["code"], error)
                    counts["errors"] += 1
        progress({"stage": "build", "kind": "cable_group", "counts": dict(counts)})
    return dict(counts)


def publish(conn, dataset_id, run_id, config, stats):
    with conn.transaction():
        lock_dataset_lifecycle(conn)
        row = conn.execute(
            "SELECT status FROM map_data.datasets WHERE id=%s FOR UPDATE", (dataset_id,)
        ).fetchone()
        if not row or row["status"] != "building":
            raise SyncError("Dataset is not available for publication")
        # Geometry validity and key uniqueness are enforced during insertion.
        conn.execute(
            "UPDATE map_data.datasets SET status='ready',published_at=now() WHERE id=%s",
            (dataset_id,),
        )
        conn.execute(
            """INSERT INTO map_data.active_datasets VALUES (%s,%s,%s)
            ON CONFLICT (source_system,map_id) DO UPDATE SET dataset_id=EXCLUDED.dataset_id""",
            (config.source_system, config.map_id, dataset_id),
        )
        conn.execute(
            """UPDATE map_data.sync_runs SET status='succeeded',finished_at=now(),stats=%s
            WHERE id=%s""",
            (Jsonb(stats), run_id),
        )
        conn.execute("DELETE FROM map_data.source_records WHERE run_id=%s", (run_id,))


def synchronize(config, *, max_errors=0, invalid_objects="fail", progress=report):
    if max_errors < 0:
        raise SyncError("max-errors must be nonnegative")
    if invalid_objects not in {"fail", "skip"}:
        raise SyncError("invalid-objects must be fail or skip")
    run_id, dataset_id = uuid4(), uuid4()
    stats = {}
    with config.connect_target() as target:
        lock_key = f"{config.source_system}:{config.map_id}"
        locked = target.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok", (lock_key,)
        ).fetchone()["ok"]
        if not locked:
            raise SyncError("A synchronization for this source/map is already running")
        try:
            # An abandoned running job cannot own this same lock anymore.
            with target.transaction():
                target.execute(
                    """UPDATE map_data.datasets SET status='failed' WHERE status='building'
                    AND source_system=%s AND map_id=%s""",
                    (config.source_system, config.map_id),
                )
                target.execute(
                    """UPDATE map_data.sync_runs SET status='interrupted',finished_at=now(),
                    error_code='interrupted' WHERE status='running' AND source_system=%s AND map_id=%s""",
                    (config.source_system, config.map_id),
                )
                target.execute(
                    """INSERT INTO map_data.sync_runs
                    (id,source_system,map_id,status,rules_version) VALUES (%s,%s,%s,'running',%s)""",
                    (run_id, config.source_system, config.map_id, RULES_VERSION),
                )
                target.execute(
                    """INSERT INTO map_data.datasets
                    (id,run_id,source_system,map_id,status,config) VALUES (%s,%s,%s,%s,'building',%s)""",
                    (
                        dataset_id,
                        run_id,
                        config.source_system,
                        config.map_id,
                        Jsonb(
                            {
                                "axes": config.axes,
                                "maxErrors": max_errors,
                                "invalidObjects": invalid_objects,
                                "cableGroupErrorPolicy": config.cable_group_error_policy,
                            }
                        ),
                    ),
                )
            progress({"stage": "start", "runId": str(run_id), "datasetId": str(dataset_id)})
            try:
                with config.connect_source() as source:
                    snapshot, extracted = copy_snapshot(
                        source, target, run_id, config.map_id, config.batch_size, progress
                    )
                stats["extracted"] = extracted
                target.execute(
                    "UPDATE map_data.sync_runs SET source_snapshot=%s WHERE id=%s",
                    (snapshot, run_id),
                )
                stats["features"] = build_features(target, dataset_id, run_id, config, progress)
                stats["excludedObjects"] = stats["features"].get("errors", 0)
                stats["invalidObjects"] = invalid_objects
                if invalid_objects == "fail" and stats["excludedObjects"] > max_errors:
                    raise SyncError(
                        "Validation failed: object errors exceed max-errors; inspect sync_issues"
                    )
                if not any(stats["features"].get(t) for t in ("node", "coupler", "cable_line")):
                    raise SyncError("Empty dataset cannot be published")
                publish(target, dataset_id, run_id, config, stats)
            except BaseException as error:
                with target.transaction():
                    target.execute(
                        "UPDATE map_data.datasets SET status='failed' WHERE id=%s AND status='building'",
                        (dataset_id,),
                    )
                    target.execute(
                        """UPDATE map_data.sync_runs SET status='failed',finished_at=now(),
                        stats=%s,error_code=%s WHERE id=%s""",
                        (Jsonb(stats), type(error).__name__, run_id),
                    )
                try:
                    paths = save_issue_report(target, run_id, config.sync_report_dir)
                    progress({"stage": "issue_report", "runId": str(run_id), "reports": paths})
                except OSError as report_error:
                    raise SyncError(
                        f"Synchronization failed ({type(error).__name__}); "
                        "the issue report could not be saved; inspect map_data.sync_issues"
                    ) from report_error
                raise
            try:
                paths = save_issue_report(target, run_id, config.sync_report_dir)
            except OSError as report_error:
                raise SyncError(
                    "Dataset published, but the issue report could not be saved; "
                    "inspect map_data.sync_issues"
                ) from report_error
            result = {
                "runId": str(run_id),
                "datasetId": str(dataset_id),
                "stats": stats,
                "reports": paths,
            }
            progress({"stage": "published", **result})
            return result
        finally:
            target.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock_key,))


def cleanup(conn, keep=2):
    if keep < 2:
        raise SyncError("Keep at least the current and previous versions")
    with conn.transaction():
        # Serialize cleanup and publication; preload must use this lock too.
        lock_dataset_lifecycle(conn)
        conn.execute("LOCK TABLE map_data.active_datasets IN SHARE ROW EXCLUSIVE MODE")
        conn.execute("DELETE FROM map_data.query_leases WHERE expires_at<=now()")
        rows = conn.execute(
            """WITH ranked AS (
            SELECT id,status,row_number() OVER (
                PARTITION BY source_system,map_id,status ORDER BY created_at DESC,id) AS rank
            FROM map_data.datasets WHERE status<>'building')
            DELETE FROM map_data.datasets d USING ranked r WHERE d.id=r.id AND r.rank>%s
            AND NOT EXISTS (SELECT 1 FROM map_data.active_datasets a WHERE a.dataset_id=d.id)
            AND NOT EXISTS (SELECT 1 FROM map_data.query_leases q WHERE q.dataset_id=d.id)
            AND NOT EXISTS (SELECT 1 FROM map_data.tile_versions v WHERE v.dataset_id=d.id
                AND v.status IN ('building','validated','ready'))
            AND NOT EXISTS (SELECT 1 FROM map_data.tile_builds b WHERE b.dataset_id=d.id
                AND b.status='running')
            RETURNING d.run_id""",
            (keep,),
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM map_data.source_records WHERE run_id=%s", (row["run_id"],))
    return len(rows)
