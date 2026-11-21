import json
from dataclasses import dataclass
from decimal import Decimal

from psycopg.types.json import Jsonb

from .config import SyncError


@dataclass(frozen=True)
class SourceTable:
    kind: str
    table: str
    fields: str
    id_column: str = "code"
    parent: str | None = None
    position: str | None = None
    where: str = "true"


# Only map-related fields are exported: no subscriber passwords or billing data.
TABLES = (
    SourceTable("map", "pbl_map", "code,typemap"),
    SourceTable(
        "node",
        "pbl_uzel",
        "code,node_type_id,parentuzel,nazv,opis,codenumber,location,isplan,mapmarkcode,ST_AsGeoJSON(coord)::json AS geometry,ST_SRID(coord) AS srid",
    ),
    SourceTable(
        "building",
        "pbl_address_building",
        "id,title,ST_AsGeoJSON(coord)::json AS geometry,ST_SRID(coord) AS srid",
        "id",
    ),
    SourceTable(
        "cable",
        "pbl_optica",
        "code,uzelcode1,uzelcode2,building2_id,cable_line_type_id,cablecode,owner_id,port,isplan,ishide,colcol,opis,link_length,optical_length",
    ),
    SourceTable(
        "place",
        "pbl_optica_house",
        "code,volscode,mapcode,position,x1,y1",
        parent="volscode",
        position="position",
    ),
    SourceTable(
        "route_object",
        "pbl_obj_int",
        "code,obj1,usercode1,obj2,usercode2,pos,dop1,additional_data_string",
        parent="usercode1",
        position="pos",
        where="obj1=1",
    ),
    SourceTable(
        "duct_content",
        "pbl_cable_duct_content",
        "id,cable_duct_id,cable_type,cable_id,position,direction",
        "id",
        "cable_id",
        "position",
        "cable_type=6",
    ),
    SourceTable("duct", "pbl_cable_duct", "id,cable_route_id", "id"),
    SourceTable(
        "route",
        "pbl_cable_route",
        "id,coord,object_first_type,object_first_id,object_second_type,object_second_id,first_building_id,second_building_id",
        "id",
    ),
    SourceTable(
        "customer",
        "pbl_base",
        "code,building_id",
        where="code IN (SELECT object_first_id FROM userside3.pbl_cable_route WHERE object_first_type=1 UNION SELECT object_second_id FROM userside3.pbl_cable_route WHERE object_second_type=1)",
    ),
    SourceTable("node_type", "pbl_node_type", "id,name,single_name,style,is_layer_on_map", "id"),
    SourceTable(
        "cable_type",
        "pbl_cable_line_type",
        "id,name,special_type,map_color,map_color_plan,map_width,is_on_map",
        "id",
    ),
    SourceTable("layer", "pbl_conf_layer", "code,nazv,onmapdef,style"),
    SourceTable(
        "layer_object", "pbl_conf_layer_object", "id,layer_id,object_uuid", "id", "layer_id"
    ),
    SourceTable(
        "mark",
        "pbl_mark_object",
        "id,object_type,object_id,mark_id",
        "id",
        "object_id",
        where="object_type IN (1,2)",
    ),
    SourceTable("cable_group", "pbl_optica_mag", "code,nazv"),
    SourceTable("mag", "pbl_optica_mag_inc", "code,volscode,magcode", parent="volscode"),
    SourceTable("group_member", "pbl_optica_mag_inc", "code,volscode,magcode", parent="magcode"),
    SourceTable("marker", "pbl_conf_map_marker", "code,nazv"),
    SourceTable(
        "setting",
        "pbl_conf",
        "id,param,valuestr,valuememo",
        "id",
        where="param IN ('VOLSM_COLOR','VOLSM_TOLS','VOLS_ALLCOLOR')",
    ),
)


def json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


def copy_snapshot(source, target, run_id, map_id, batch_size, progress):
    snapshot = source.execute("SELECT txid_current_snapshot()::text AS snapshot").fetchone()
    source.execute("SET LOCAL statement_timeout = '60s'")
    row = source.execute(
        "SELECT typemap FROM userside3.pbl_map WHERE code=%s", (map_id,)
    ).fetchone()
    if not row or row["typemap"] != 3:
        raise SyncError("Source map is absent or is not a geographic map")
    counts = {}
    for spec in TABLES:
        # Table definitions are fixed application code, never request inputs.
        with source.cursor(name="export_" + spec.kind) as cursor:
            cursor.execute(
                f"SELECT {spec.fields} FROM userside3.{spec.table} WHERE {spec.where} ORDER BY {spec.id_column}"
            )
            count = 0
            while rows := cursor.fetchmany(batch_size):
                with target.transaction(), target.cursor() as writer:
                    writer.executemany(
                        """INSERT INTO map_data.source_records
                            (run_id,kind,source_id,parent_id,position,data)
                            VALUES (%s,%s,%s,%s,%s,%s)""",
                        [
                            (
                                run_id,
                                spec.kind,
                                row[spec.id_column],
                                row.get(spec.parent),
                                row.get(spec.position),
                                Jsonb(row, dumps=lambda v: json.dumps(v, default=json_default)),
                            )
                            for row in rows
                        ],
                    )
                count += len(rows)
            counts[spec.kind] = count
            progress({"stage": "extract", "kind": spec.kind, "count": count})
    return snapshot["snapshot"], counts
