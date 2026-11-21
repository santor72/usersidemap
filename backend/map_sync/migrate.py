from hashlib import sha256
from importlib.resources import files

from .config import SyncError


def migrate(conn):
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(79124001)")
        conn.execute("CREATE SCHEMA IF NOT EXISTS map_data")
        conn.execute("""CREATE TABLE IF NOT EXISTS map_data.schema_migrations (
            name text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        for path in sorted(
            files("map_sync").joinpath("migrations").iterdir(), key=lambda p: p.name
        ):
            if not path.name.endswith(".sql"):
                continue
            sql = path.read_text()
            digest = sha256(sql.encode()).hexdigest()
            previous = conn.execute(
                "SELECT checksum FROM map_data.schema_migrations WHERE name=%s", (path.name,)
            ).fetchone()
            if previous:
                if previous["checksum"] != digest:
                    raise SyncError("Applied migration has changed: " + path.name)
                continue
            conn.execute(sql, prepare=False)
            conn.execute(
                "INSERT INTO map_data.schema_migrations VALUES (%s,%s,now())", (path.name, digest)
            )
            applied.append(path.name)
    return applied
