import argparse
import os
from pathlib import Path
from uuid import UUID

import psycopg

from .config import Config, SyncError
from .geometry import ObjectError
from .issue_report import save_issue_report
from .migrate import migrate
from .sync import cleanup, report, synchronize


def main():
    parser = argparse.ArgumentParser(description="Userside to versioned PostGIS synchronization")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sync = sub.add_parser("sync")
    sync.add_argument(
        "--invalid-objects",
        choices=("fail", "skip"),
        default=os.environ.get("SYNC_INVALID_OBJECTS", "fail"),
        help="Fail on object errors or publish valid objects and report excluded ones",
    )
    sync.add_argument(
        "--max-errors",
        type=int,
        default=0,
        help="Explicit maximum number of invalid objects allowed in a published version",
    )
    gc = sub.add_parser("cleanup")
    gc.add_argument("--keep", type=int, default=2)
    issue_report = sub.add_parser("report", help="Export an existing synchronization issue report")
    issue_report.add_argument("run_id", type=UUID)
    args = parser.parse_args()
    try:
        config = Config.load(args.env_file, require_source=args.command == "sync")
        if args.command == "sync":
            synchronize(config, max_errors=args.max_errors, invalid_objects=args.invalid_objects)
        else:
            with config.connect_target() as conn:
                if args.command == "migrate":
                    report({"applied": migrate(conn)})
                elif args.command == "report":
                    report(
                        {"reports": save_issue_report(conn, args.run_id, config.sync_report_dir)}
                    )
                else:
                    report({"deletedDatasets": cleanup(conn, args.keep)})
    except (SyncError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    except ObjectError as error:
        parser.exit(1, f"Invalid source configuration ({error.code}): {error.detail}\n")
    except psycopg.Error as error:
        # Never print a DSN, password, or driver error containing source row data.
        parser.exit(
            1,
            f"Database operation failed ({type(error).__name__}, SQLSTATE {error.sqlstate}); connection details omitted\n",
        )
    except OSError:
        parser.exit(1, "Cannot save issue report; check SYNC_REPORT_DIR and write permissions\n")


if __name__ == "__main__":
    main()
