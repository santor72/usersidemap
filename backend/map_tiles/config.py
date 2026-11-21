"""Explicit worker settings; never fall back to the API or source credentials."""

import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

from .defaults import CONCURRENCY, DISPLAY_MAXZOOM, RASTER_PREBAKE_MAX, VECTOR_MAXZOOM
from .storage import root_from_env


@dataclass(frozen=True)
class TileConfig:
    database_url: str
    root: Path
    source_system: str = "userside"
    map_id: int = 3
    minzoom: int = 1
    vector_maxzoom: int = VECTOR_MAXZOOM
    display_maxzoom: int = DISPLAY_MAXZOOM
    concurrency: int = CONCURRENCY
    cable_type_ids: tuple = (1,)
    redis_url: str | None = None
    raster_prebake_max: int = RASTER_PREBAKE_MAX
    tasks_per_process: int = 64

    def __post_init__(self):
        if not self.database_url or not self.source_system or self.map_id <= 0:
            raise ValueError("Set worker database URL, source and positive map ID")
        if not 0 <= self.minzoom <= 14 <= self.vector_maxzoom <= self.display_maxzoom <= 24:
            raise ValueError("Invalid zoom range; native vector maxzoom must be at least 14")
        if not 15 <= self.raster_prebake_max <= min(24, self.display_maxzoom + 1):
            raise ValueError("Raster native maxzoom must be >=15 to include logical zoom14 cables")
        if not 1 <= self.tasks_per_process <= 1024:
            raise ValueError("Tasks per process must be between 1 and 1024")
        if not 1 <= self.concurrency <= 8:
            raise ValueError("Tile concurrency must be between 1 and 8")
        if not self.cable_type_ids or any(
            type(n) is not int or n <= 0 for n in self.cable_type_ids
        ):
            raise ValueError("Cable types must be positive integer IDs")

    @classmethod
    def load(cls, env_file=Path(".env"), *, format="vector"):
        if format not in ("vector", "raster"):
            raise ValueError("Unknown worker format")
        env = {**dotenv_values(env_file), **os.environ}
        return cls(
            env.get(f"MAP_TILES_{format.upper()}_DATABASE_URL", ""),
            root_from_env(env, Path(env.get("TILES_ROOT", "./data/tiles")).resolve()),
            env.get("US_SOURCE_SYSTEM", "userside"),
            int(env.get("US_MAP_ID", "3")),
            int(env.get("TILES_MINZOOM", "1")),
            int(env.get("TILES_VECTOR_MAXZOOM", str(VECTOR_MAXZOOM))),
            int(env.get("TILES_DISPLAY_MAXZOOM", str(DISPLAY_MAXZOOM))),
            int(env.get("TILES_CONCURRENCY", str(CONCURRENCY))),
            tuple(int(n) for n in env.get("TILES_CABLE_TYPE_IDS", "1").split(",")),
            env.get("MAP_REDIS_URL"),
            int(env.get("TILES_RASTER_PREBAKE_MAX", str(RASTER_PREBAKE_MAX))),
            int(env.get("TILES_TASKS_PER_PROCESS", "64")),
        )

    def connect(self):
        conn = psycopg.connect(
            self.database_url, autocommit=True, row_factory=dict_row, connect_timeout=5
        )
        conn.execute("SET statement_timeout=30000")
        if conn.execute("SELECT to_regclass('userside3.pbl_optica') AS name").fetchone()["name"]:
            conn.close()
            raise ValueError("Worker database must be the separate snapshot database")
        return conn
