"""Inspect Userside metadata without displaying connection credentials."""

import argparse
import json
import os
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    config = {
        **dotenv_values(Path(__file__).resolve().parents[1] / ".env"),
        **os.environ,
    }
    required = ("US_DB_HOST", "US_DB_NAME", "US_DB_USER", "US_DB_PASS")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise SystemExit("Missing configuration keys: " + ", ".join(missing))
    try:
        connection = psycopg.connect(
            host=config["US_DB_HOST"],
            port=config.get("US_DB_PORT", "5432"),
            dbname=config["US_DB_NAME"],
            user=config["US_DB_USER"],
            password=config["US_DB_PASS"],
            connect_timeout=10,
            options="-c default_transaction_read_only=on -c statement_timeout=15000",
            row_factory=dict_row,
        )
    except psycopg.Error as exc:
        # Driver messages can contain hostnames and connection parameters.
        raise SystemExit(
            f"Connection failed ({type(exc).__name__}); credentials omitted."
        ) from None
    tables = [
        "pbl_uzel",
        "pbl_optica",
        "pbl_optica_house",
        "pbl_obj_int",
        "pbl_cable_duct",
        "pbl_cable_duct_content",
        "pbl_cable_route",
        "pbl_node_type",
        "pbl_cable_line_type",
        "pbl_mark_object",
        "pbl_conf_layer",
        "pbl_conf_layer_object",
    ]
    with connection, connection.cursor() as cursor:
        queries = {
            "extensions": ("SELECT extname, extversion FROM pg_extension", ()),
            "columns": (
                """SELECT table_name, column_name, data_type, udt_name,
                is_nullable FROM information_schema.columns
                WHERE table_schema = 'userside3' AND table_name = ANY(%s)
                ORDER BY table_name, ordinal_position""",
                (tables,),
            ),
            "indexes": (
                """SELECT tablename, indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'userside3' AND tablename = ANY(%s)
                ORDER BY tablename, indexname""",
                (tables,),
            ),
            "map": (
                "SELECT code, typemap FROM userside3.pbl_map WHERE code = 3",
                (),
            ),
            "node_geometry": (
                """SELECT ST_SRID(coord) AS srid,
                GeometryType(coord) AS geometry_type, count(*) AS count
                FROM userside3.pbl_uzel WHERE coord IS NOT NULL
                GROUP BY 1, 2""",
                (),
            ),
            "node_types": (
                """SELECT node_type_id, count(*) AS count,
                count(*) FILTER (WHERE coord IS NULL) AS without_coord
                FROM userside3.pbl_uzel GROUP BY node_type_id ORDER BY node_type_id""",
                (),
            ),
            "cables": ("SELECT count(*) AS count FROM userside3.pbl_optica", ()),
            "axis_evidence": (
                """WITH points AS (
                SELECT u.code, ST_X(u.coord) AS x, ST_Y(u.coord) AS y
                FROM userside3.pbl_uzel u WHERE GeometryType(u.coord) = 'POINT')
                SELECT count(*) AS compared,
                count(*) FILTER (WHERE abs(p.x-h.y1)<0.001 AND abs(p.y-h.x1)<0.001) AS legacy_order_matches,
                count(*) FILTER (WHERE abs(p.x-h.x1)<0.001 AND abs(p.y-h.y1)<0.001) AS same_order_matches
                FROM userside3.pbl_optica o JOIN points p ON p.code=o.uzelcode1
                JOIN LATERAL (SELECT x1,y1 FROM userside3.pbl_optica_house
                    WHERE volscode=o.code ORDER BY position,code LIMIT 1) h ON true""",
                (),
            ),
            "extra_columns": (
                """SELECT table_name,column_name,udt_name
                FROM information_schema.columns WHERE table_schema='userside3'
                AND table_name IN ('pbl_conf','pbl_address_building','pbl_optica_mag_inc', 'pbl_map_marker', 'pbl_customer')
                ORDER BY table_name,ordinal_position""",
                (),
            ),
        }
        result = {}
        for label, (sql, params) in queries.items():
            with connection.transaction():
                cursor.execute(sql, params)
                result[label] = cursor.fetchall()
        result["columns"] = {
            table: {
                row["column_name"]: row["udt_name"]
                for row in result["columns"]
                if row["table_name"] == table
            }
            for table in tables
        }
        if args.summary:
            result.pop("columns")
            result.pop("indexes")
            result["extra_columns"] = {
                table: [
                    r["column_name"]
                    for r in result["extra_columns"]
                    if r["table_name"] == table
                ]
                for table in {r["table_name"] for r in result["extra_columns"]}
            }
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
