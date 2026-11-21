"""One container gets exactly one destination worker role; no owner/source DSN."""

import os
import sys
from urllib.parse import quote


def main():
    format = os.environ.get("MAP_TILES_FORMAT")
    if format not in ("vector", "raster"):
        sys.exit("Set MAP_TILES_FORMAT=vector|raster")
    password = os.environ.get(f"MAP_TILES_{format.upper()}_DB_PASSWORD")
    if not password:
        sys.exit("Set the worker database password")
    os.environ[f"MAP_TILES_{format.upper()}_DATABASE_URL"] = (
        f"postgresql://map_tiles_{format}:{quote(password, safe='')}@db:5432/map"
    )
    os.execvp("map-tiles", ["map-tiles", *sys.argv[1:]])


if __name__ == "__main__":
    main()
