"""Container commands: derive internal DSNs without storing source credentials in API."""

import os
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg
from map_sync.config import Config, SyncError
from map_sync.grants import grant_tile_roles, set_worker_password
from map_sync.migrate import migrate
from psycopg import sql


def database_url(user, password):
    if not password:
        raise SyncError("Set the required database password")
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@db:5432/map"


def initialize():
    config = Config.load(Path("/nonexistent.env"), require_source=False)
    with config.connect_target() as conn:
        applied = migrate(conn)
        # Migrations and grants are complete before Compose starts the API.
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(79124001)")
            if not conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'map_api'"
            ).fetchone():
                conn.execute("CREATE ROLE map_api LOGIN")
            conn.execute(
                sql.SQL(
                    "ALTER ROLE map_api WITH LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION PASSWORD {}"
                ).format(sql.Literal(os.environ["MAP_API_DB_PASSWORD"]))
            )
            conn.execute("GRANT CONNECT ON DATABASE map TO map_api")
            conn.execute("GRANT USAGE ON SCHEMA map_data TO map_api")
            conn.execute("""GRANT SELECT ON map_data.datasets, map_data.active_datasets,
                map_data.features, map_data.layers, map_data.feature_layers,
                map_data.feature_links TO map_api""")
            conn.execute("""GRANT SELECT, INSERT, UPDATE, DELETE ON map_data.query_leases,
                map_data.query_batches, map_data.query_items,
                map_data.query_lease_totals TO map_api""")
            conn.execute("""GRANT EXECUTE ON FUNCTION
                map_data.api_pin_active_dataset(text, bigint) TO map_api""")
            grant_tile_roles(conn)
            for format in ("vector", "raster"):
                set_worker_password(
                    conn,
                    format,
                    os.environ.get(f"MAP_TILES_{format.upper()}_DB_PASSWORD"),
                )
        print(f"Database initialized; {len(applied)} migrations applied", flush=True)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "api"
    if command == "api":
        os.environ["MAP_API_DATABASE_URL"] = database_url(
            "map_api", os.environ.get("MAP_API_DB_PASSWORD")
        )
        os.environ["MAP_API_HOST"] = "0.0.0.0"
        os.environ["MAP_API_PORT"] = "8000"
        os.execvp("map-api", ["map-api"])
    if command == "tokens":
        os.environ["MAP_TOKEN_ADMIN_DATABASE_URL"] = database_url(
            "map", os.environ.get("MAP_DB_PASSWORD")
        )
        os.execvp("map-token", ["map-token", *sys.argv[2:]])
    if command not in {"init", "sync", "cleanup", "report"}:
        raise SyncError("Expected api, init, sync, cleanup, report or tokens")
    os.environ["MAP_DB_URL"] = database_url("map", os.environ.get("MAP_DB_PASSWORD"))
    if command == "init":
        if not os.environ.get("MAP_API_DB_PASSWORD"):
            raise SyncError("Set MAP_API_DB_PASSWORD")
        initialize()
    else:
        os.execvp("map-db", ["map-db", command, *sys.argv[2:]])


if __name__ == "__main__":
    try:
        main()
    except (SyncError, ValueError) as error:
        sys.exit(str(error))
    except psycopg.Error as error:
        sys.exit(
            f"Database operation failed ({type(error).__name__}, "
            f"SQLSTATE {error.sqlstate}); connection details omitted"
        )
