"""Deployment role grants, shared with disposable privilege integration tests."""

from psycopg import sql


def grant_tile_roles(conn):
    """Caller owns migration schema. Passwords are configured separately."""
    for role in ("map_tiles_vector", "map_tiles_raster"):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                ).format(sql.Identifier(role))
            )
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA map_data TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("""GRANT SELECT ON map_data.datasets,map_data.features,
            map_data.layers,map_data.feature_layers,map_data.feature_links TO {}""").format(
                sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("""GRANT SELECT,INSERT,UPDATE ON map_data.tile_builds,
            map_data.tile_versions,map_data.active_tile_versions TO {}""").format(
                sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION map_data.tiles_pin_dataset(text,bigint,uuid) TO {}"
            ).format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(conn.info.dbname), sql.Identifier(role)
            )
        )
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname='map_api'").fetchone():
        conn.execute("""GRANT SELECT ON map_data.tile_versions,map_data.active_tile_versions,
            map_data.tile_builds,map_data.api_tokens TO map_api""")
        conn.execute(
            "GRANT EXECUTE ON FUNCTION map_data.api_pin_tile_dataset(text,bigint,text,uuid) TO map_api"
        )


def set_worker_password(conn, format, password):
    if format not in ("vector", "raster"):
        raise ValueError("Unknown worker format")
    role = sql.Identifier("map_tiles_" + format)
    if password:
        conn.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(role, sql.Literal(password))
        )
    else:
        conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN NOBYPASSRLS").format(role))
