import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row


class SyncError(Exception):
    """A safe diagnostic suitable for CLI output."""


@dataclass(frozen=True)
class Config:
    source: dict
    target_url: str
    map_id: int = 3
    batch_size: int = 500
    source_system: str = "userside"
    axes: str = "lat_lon"
    cable_group_error_policy: str = "bridge"
    sync_report_dir: Path = Path("reports/sync")

    def __post_init__(self):
        if self.cable_group_error_policy not in ("bridge", "drop"):
            raise SyncError("US_CABLE_GROUP_ERROR_POLICY must be bridge or drop")

    @classmethod
    def load(cls, env_file: Path, *, require_source=True):
        env = {**dotenv_values(env_file), **os.environ}
        keys = ("US_DB_HOST", "US_DB_NAME", "US_DB_USER", "US_DB_PASS")
        if require_source and any(not env.get(k) for k in keys):
            raise SyncError("Set US_DB_HOST, US_DB_NAME, US_DB_USER, US_DB_PASS")
        url = env.get("MAP_DB_URL")
        if not url:
            raise SyncError("Set MAP_DB_URL for the separate destination database")
        axes = env.get("US_COORD_AXES", "lat_lon")
        if axes not in ("lat_lon", "lon_lat"):
            raise SyncError("US_COORD_AXES must be lat_lon or lon_lat")
        size = int(env.get("SYNC_BATCH_SIZE", "500"))
        if not 1 <= size <= 10000:
            raise SyncError("SYNC_BATCH_SIZE must be between 1 and 10000")
        return cls(
            source={
                "host": env.get("US_DB_HOST"),
                "dbname": env.get("US_DB_NAME"),
                "user": env.get("US_DB_USER"),
                "password": env.get("US_DB_PASS"),
                "port": env.get("US_DB_PORT", "5432"),
            },
            target_url=url,
            map_id=int(env.get("US_MAP_ID", "3")),
            batch_size=size,
            source_system=env.get("US_SOURCE_SYSTEM", "userside"),
            axes=axes,
            cable_group_error_policy=env.get("US_CABLE_GROUP_ERROR_POLICY", "bridge"),
            sync_report_dir=env_file.parent / (env.get("SYNC_REPORT_DIR") or "reports/sync"),
        )

    def connect_source(self):
        conn = psycopg.connect(**self.source, connect_timeout=10, row_factory=dict_row)
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        return conn

    def connect_target(self):
        conn = psycopg.connect(
            self.target_url, connect_timeout=10, autocommit=True, row_factory=dict_row
        )
        if conn.execute("SELECT to_regclass('userside3.pbl_optica') AS table_name").fetchone()[
            "table_name"
        ]:
            conn.close()
            raise SyncError("Destination contains Userside tables; refusing writes")
        return conn
