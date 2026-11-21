"""Admin-only token create/revoke/list; plaintext is returned once."""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

from .tile_auth import issue_token


def main(argv=None):
    parser = argparse.ArgumentParser(prog="map-token")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--map", type=int, action="append", required=True)
    create.add_argument("--scope", choices=["metadata", "details"], action="append", required=True)
    create.add_argument("--ttl-seconds", type=int, default=2592000)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--id", required=True)
    commands.add_parser("list")
    args = parser.parse_args(argv)
    try:
        env = {**dotenv_values(args.env), **os.environ}
        url = env.get("MAP_TOKEN_ADMIN_DATABASE_URL")
        if not url:
            raise ValueError("Set MAP_TOKEN_ADMIN_DATABASE_URL to the admin snapshot database")
        with psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
            if conn.execute("SELECT to_regclass('userside3.pbl_optica') AS name").fetchone()[
                "name"
            ]:
                raise ValueError("Admin destination must not be Userside")
            if args.command == "create":
                result = issue_token(conn, args.name, args.map, args.scope, args.ttl_seconds)
            elif args.command == "revoke":
                row = conn.execute(
                    "UPDATE map_data.api_tokens SET revoked_at=clock_timestamp() WHERE id=%s RETURNING id",
                    (args.id,),
                ).fetchone()
                result = {"revoked": bool(row), "id": args.id}
            else:
                result = conn.execute(
                    "SELECT id,name,map_ids,scopes,created_at,expires_at,revoked_at FROM map_data.api_tokens ORDER BY created_at"
                ).fetchall()
        print(json.dumps(result, default=str))
        return 0
    except (ValueError, psycopg.Error):
        print("TOKEN_ADMIN_FAILED: check admin database/settings", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
