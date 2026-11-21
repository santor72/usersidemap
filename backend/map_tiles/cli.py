"""Manual bake commands require an explicit snapshot UUID."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

import psycopg

from .config import TileConfig
from .lifecycle import TileError
from .pipeline import bake_raster, bake_vector
from .publication import operate


def main(argv=None):
    parser = argparse.ArgumentParser(prog="map-tiles")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    vector = commands.add_parser("vector", help="Full vector staging bake; no active publication")
    vector.add_argument("--dataset", type=UUID, required=True)
    raster = commands.add_parser("raster", help="Full raster 1x/@2x staging bake")
    raster.add_argument("--dataset", type=UUID, required=True)
    for command in ("publish", "rollback", "cleanup"):
        action = commands.add_parser(command)
        action.add_argument("--format", choices=["vector", "raster"], required=True)
        if command in ("publish", "rollback"):
            action.add_argument("--version", type=UUID, required=command == "publish")
    migration = commands.add_parser(
        "migrate-storage", help="Copy and verify retained local releases into S3"
    )
    migration.add_argument("--format", choices=["vector", "raster"], required=True)
    migration.add_argument("--source-root", type=Path, default=Path("/tiles"))
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate-storage":
            from .migrate_storage import migrate

            result = {
                "migrated": migrate(
                    TileConfig.load(args.env, format=args.format),
                    args.source_root,
                    progress=lambda row: print(row, flush=True),
                )
            }
        elif args.command in ("vector", "raster"):
            bake = bake_vector if args.command == "vector" else bake_raster
            result = bake(
                TileConfig.load(args.env, format=args.command),
                args.dataset,
                progress=lambda row: print(json.dumps({"event": "zoom", **row}), flush=True),
            )
        else:
            result = operate(
                TileConfig.load(args.env, format=args.format),
                args.format,
                args.command,
                getattr(args, "version", None),
            )
        print(json.dumps(result), flush=True)
        return 2 if result.get("cleanup", {}).get("cleanupPending") else 0
    except KeyboardInterrupt:
        print("BUILD_CANCELLED: build recorded; inspect cleanup status", file=sys.stderr)
        return 130
    except TileError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(f"CONFIG_OR_FILE_ERROR: {type(error).__name__}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"DATABASE_ERROR: {type(error).__name__} SQLSTATE={error.sqlstate}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- never print worker connection secrets
        print(f"BUILD_FAILED: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
