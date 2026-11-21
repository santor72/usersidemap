#!/usr/bin/env python3
"""Cron-friendly sync → vector → raster → publish, using separate Compose roles."""

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]


def receive_line(line, receive):
    try:
        row = json.loads(line)
    except (ValueError, TypeError):
        if line.startswith("Validation failed: object errors exceed max-errors"):
            receive({"event": "command_error", "code": "SYNC_VALIDATION_FAILED"})
        elif re.match(r"^[A-Z][A-Z0-9_]{2,40}:", line):
            receive({"event": "command_error", "code": line.split(":", 1)[0]})
        return
    if isinstance(row, dict):
        receive(row)


class UpdateError(Exception):
    def __init__(self, code, message, exit_code=1):
        self.code, self.exit_code = code, exit_code
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    """Atomic private checkpoint; committed before deleting a job container."""
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with os.fdopen(
            os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Compose:
    def __init__(self, root):
        self.prefix = [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "compose.yml"),
            "--profile",
            "tools",
        ]

    def quiet(self, args):
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode:
            raise UpdateError(
                "DOCKER_COMMAND_FAILED", "Docker command failed; check Docker and .env"
            )
        return result.stdout

    def scope(self):
        services = json.loads(self.quiet([*self.prefix, "config", "--format", "json"]))[
            "services"
        ]
        env = services["jobs"]["environment"]
        scope = {
            "sourceSystem": env["US_SOURCE_SYSTEM"],
            "mapId": int(env["US_MAP_ID"]),
        }
        for format in ("vector", "raster"):
            worker = services["tiles-" + format]["environment"]
            if not worker.get("MAP_TILES_" + format.upper() + "_DB_PASSWORD"):
                raise UpdateError(
                    "WORKER_PASSWORD_MISSING", "Configure both worker passwords in .env"
                )
            if (
                worker["US_SOURCE_SYSTEM"] != scope["sourceSystem"]
                or int(worker["US_MAP_ID"]) != scope["mapId"]
            ):
                raise UpdateError(
                    "MAP_SCOPE_MISMATCH",
                    "Sync and both workers must use the same source/map",
                )
        return scope

    def prepare(self):
        self.quiet([*self.prefix, "up", "-d", "db", "redis"])
        self.quiet([*self.prefix, "run", "--rm", "--no-deps", "migrate"])

    def inspect(self, name):
        result = subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, check=False
        )
        return json.loads(result.stdout)[0]["State"] if result.returncode == 0 else None

    def remove(self, name):
        state = self.inspect(name)
        if state:
            if state["Running"]:
                raise UpdateError(
                    "CONTAINER_RUNNING",
                    "A previous step is still running; retry later",
                    75,
                )
            self.quiet(["docker", "rm", name])

    def stop(self, name):
        subprocess.run(
            ["docker", "stop", "--time", "30", name], capture_output=True, check=False
        )

    def execute(self, name, service, args, retry, receive):
        state = self.inspect(name)
        if retry and state and not state["Running"] and state["ExitCode"] != 0:
            self.remove(name)
            state = None
        command = (
            ["docker", "logs", "--follow", name]
            if state
            else [*self.prefix, "run", "--name", name, "--no-deps", service, *args]
        )
        # Keep the container until its result is checkpointed. A killed wrapper
        # can recover final JSON and ExitCode from it on the next cron invocation.
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            for line in process.stdout:
                receive_line(line, receive)
            code = process.wait()
            state = self.inspect(name)
            if state and state["Running"]:
                raise UpdateError(
                    "CONTAINER_RUNNING",
                    "Worker continues; the next invocation will reattach",
                    75,
                )
            return state["ExitCode"] if state else code
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


# The supervisor commits an exit receipt independently of the orchestrator.
# If the container stops before the receipt is committed, rerun that step.
SUPERVISE = """
import os, subprocess, sys
from pathlib import Path
receipt, log = map(Path, sys.argv[1:3])
with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as out:
    code = subprocess.run(sys.argv[3:], stdout=out, stderr=subprocess.STDOUT).returncode
    out.flush()
    os.fsync(out.fileno())
temp = receipt.with_suffix('.tmp')
with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as out:
    out.write(str(code))
    out.flush()
    os.fsync(out.fileno())
os.replace(temp, receipt)
"""


class LocalWorker:
    """Run installed CLIs inside the worker, without Docker access."""

    def __init__(self, directory):
        self.directory = directory
        self.process = None

    def scope(self):
        for key in (
            "MAP_DB_PASSWORD",
            "MAP_TILES_VECTOR_DB_PASSWORD",
            "MAP_TILES_RASTER_DB_PASSWORD",
        ):
            if not os.environ.get(key):
                raise UpdateError(
                    "WORKER_PASSWORD_MISSING", "Configure worker passwords"
                )
        return {
            "sourceSystem": os.environ.get("US_SOURCE_SYSTEM", "userside"),
            "mapId": int(os.environ.get("US_MAP_ID", "3")),
        }

    def prepare(self):
        # Compose waits for db health and the separate migration service.
        (self.directory / "sync").mkdir(exist_ok=True, mode=0o700)

    def paths(self, name):
        return self.directory / (name + ".log"), self.directory / (name + ".exit")

    def remove(self, name):
        for path in self.paths(name):
            path.unlink(missing_ok=True)

    def stop(self, name):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)

    def command(self, service, args):
        # Source/owner credentials never enter tile subprocess environments.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(
                ("US_DB_", "MAP_DB_", "MAP_TILES_", "MAP_API_", "NUXT_", "TILES_S3_")
            )
        }
        host = os.environ.get("MAP_DB_HOST", "db")
        if service == "jobs":
            env.update({k: v for k, v in os.environ.items() if k.startswith("US_DB_")})
            password = quote(os.environ["MAP_DB_PASSWORD"], safe="")
            env["MAP_DB_URL"] = f"postgresql://map:{password}@{host}:5432/map"
            env["SYNC_REPORT_DIR"] = str(self.directory / "sync")
            return ["map-db", *args], env
        format = service.removeprefix("tiles-")
        if format not in ("vector", "raster"):
            raise UpdateError("UNKNOWN_WORKER", "Unknown tile worker")
        env.update({k: os.environ[k] for k in (
            "TILES_S3_ENDPOINT", "TILES_S3_BUCKET", "TILES_S3_REGION",
            "TILES_S3_ACCESS_KEY", "TILES_S3_SECRET_KEY",
        ) if k in os.environ})
        key = "MAP_TILES_" + format.upper()
        password = quote(os.environ[key + "_DB_PASSWORD"], safe="")
        env[key + "_DATABASE_URL"] = (
            f"postgresql://map_tiles_{format}:{password}@{host}:5432/map"
        )
        return ["map-tiles", *args], env

    def execute(self, name, service, args, retry, receive):
        log, receipt = self.paths(name)
        if receipt.exists() and log.exists():
            code = int(receipt.read_text())
            if not retry or code == 0:
                with log.open() as stream:
                    for line in stream:
                        receive_line(line, receive)
                return code
        self.remove(name)
        command, env = self.command(service, args)
        self.process = subprocess.Popen(
            [sys.executable, "-c", SUPERVISE, str(receipt), str(log), *command],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            while not log.exists():
                if self.process.poll() is not None:
                    raise UpdateError("STEP_START_FAILED", "Cannot start worker CLI")
                time.sleep(0.05)
            with log.open() as stream:
                while True:
                    line = stream.readline()
                    if line:
                        receive_line(line, receive)
                    elif self.process.poll() is not None:
                        # Drain output written between readline and poll.
                        for line in stream:
                            receive_line(line, receive)
                        break
                    else:
                        time.sleep(0.1)
            if not receipt.exists():
                raise UpdateError(
                    "STEP_INTERRUPTED", "Worker exited without an exit receipt"
                )
            return int(receipt.read_text())
        finally:
            self.stop(name)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            self.process = None


def result_for(step, row):
    """Persist only the CLI contract, never raw output, credentials or source rows."""
    if step == "sync" and row.get("stage") == "published":
        return {
            "datasetId": str(UUID(row["datasetId"])),
            "runId": str(UUID(row["runId"])),
            "excludedObjects": int(row.get("stats", {}).get("excludedObjects", 0)),
        }
    if step in ("vector", "raster") and row.get("status") == "validated":
        return {
            "tileVersion": str(UUID(row["tileVersion"])),
            "datasetVersion": str(UUID(row["datasetVersion"])),
            "format": row["format"],
        }
    if step.startswith("publish-") and row.get("published") is True:
        return {
            "version": str(UUID(row["version"])),
            "datasetVersion": str(UUID(row["datasetVersion"])),
            "published": True,
            "cleanupPending": bool(row.get("cleanup", {}).get("cleanupPending")),
        }
    if step in ("cleanup-vector", "cleanup-raster") and "cleanupPending" in row:
        return {"cleanupPending": bool(row["cleanupPending"])}
    return None


def cycle(runner, directory, max_errors=0, invalid_objects="skip"):
    scope = runner.scope()
    latest = directory / "latest.json"
    job = json.loads(latest.read_text()) if latest.exists() else None
    if job and job["status"] != "completed":
        if job["scope"] != scope:
            raise UpdateError(
                "RESUME_SCOPE_MISMATCH",
                "Finish the pending cycle before changing source/map",
            )
        print("Продолжаю цикл " + job["id"], flush=True)
        if job["steps"].get("sync", {}).get("status") != "succeeded":
            job["maxErrors"] = max_errors
            job["invalidObjects"] = invalid_objects
    else:
        job = {
            "schemaVersion": 1,
            "id": str(uuid4()),
            "scope": scope,
            "createdAt": now(),
            "status": "running",
            "datasetId": None,
            "maxErrors": max_errors,
            "invalidObjects": invalid_objects,
            "steps": {},
        }
        print("Новый цикл " + job["id"], flush=True)
    journal = directory / (job["id"] + ".json")

    def checkpoint():
        job["updatedAt"] = now()
        save(journal, job)
        save(latest, job)

    active = None
    try:
        job["status"] = "running"
        job.pop("error", None)
        checkpoint()
        runner.prepare()
        for step in (
            "sync",
            "vector",
            "raster",
            "publish-vector",
            "publish-raster",
            "cleanup-vector",
            "cleanup-raster",
        ):
            record = job["steps"].get(step)
            if record and record["status"] == "succeeded":
                runner.remove(record["container"])
                continue
            name = "map-update-" + UUID(job["id"]).hex[:16] + "-" + step
            retry = bool(record and record["status"] != "succeeded")
            record = {
                "status": "running",
                "container": name,
                "attempts": (record or {}).get("attempts", 0) + 1,
            }
            job["steps"][step] = record
            active = record
            checkpoint()
            if step == "sync":
                service, args = (
                    "jobs",
                    [
                        "sync",
                        "--max-errors",
                        str(job["maxErrors"]),
                        "--invalid-objects",
                        job["invalidObjects"],
                    ],
                )
            elif step in ("vector", "raster"):
                service, args = "tiles-" + step, [step, "--dataset", job["datasetId"]]
            elif step.startswith("publish-"):
                format = step.removeprefix("publish-")
                service, args = (
                    "tiles-" + format,
                    [
                        "publish",
                        "--format",
                        format,
                        "--version",
                        job["steps"][format]["result"]["tileVersion"],
                    ],
                )
            else:
                format = step.removeprefix("cleanup-")
                service, args = "tiles-" + format, ["cleanup", "--format", format]
            print(step + ": запуск", flush=True)
            started = time.monotonic()

            def receive(row, step=step, record=record):
                if row.get("event") == "command_error":
                    record["errorCode"] = row["code"]
                    checkpoint()
                if step == "sync" and row.get("stage") == "start":
                    record["syncRunId"] = str(UUID(row["runId"]))
                    record["candidateDatasetId"] = str(UUID(row["datasetId"]))
                    checkpoint()
                result = result_for(step, row)
                if result is not None:
                    record["result"] = result
                    checkpoint()
                elif type(row.get("z")) is int:
                    print(f"  {step}: zoom {row['z']}", flush=True)

            code = runner.execute(name, service, args, retry, receive)
            record["seconds"] = round(time.monotonic() - started, 3)
            result = record.get("result")
            published_pending = (
                step.startswith("publish-")
                and code == 2
                and result
                and result["published"]
            )
            if (code != 0 and not published_pending) or not result:
                record["status"] = "failed"
                record["exitCode"] = code
                checkpoint()
                raise UpdateError(
                    "STEP_FAILED",
                    f"{step}: {record.get('errorCode', 'COMMAND_FAILED')}, exit {code}; "
                    + (
                        f"log {runner.paths(name)[0]}"
                        if isinstance(runner, LocalWorker)
                        else f"container {name}"
                    ),
                    2 if code == 2 else 1,
                )
            if step == "sync":
                job["datasetId"] = result["datasetId"]
                print(
                    f"sync: исключено некорректных объектов: {result['excludedObjects']}",
                    flush=True,
                )
            elif step in ("vector", "raster"):
                if (
                    result["datasetVersion"] != job["datasetId"]
                    or result["format"] != step
                ):
                    raise UpdateError(
                        "DATASET_MISMATCH",
                        "Worker result differs from the cycle snapshot",
                    )
            elif step.startswith("publish-"):
                format = step.removeprefix("publish-")
                if (
                    result["datasetVersion"] != job["datasetId"]
                    or result["version"]
                    != job["steps"][format]["result"]["tileVersion"]
                ):
                    raise UpdateError(
                        "PUBLICATION_MISMATCH",
                        "Publication returned a different release",
                    )
            elif result.get("cleanupPending"):
                raise UpdateError(
                    "CLEANUP_PENDING",
                    "Cleanup must complete before the next snapshot",
                    2,
                )
            record["status"] = "succeeded"
            record["exitCode"] = code
            checkpoint()
            runner.remove(name)
            active = None
            print(step + ": готово", flush=True)
        job["status"] = "completed"
        job["finishedAt"] = now()
        checkpoint()
        print(
            "Готово. Dataset: " + job["datasetId"] + "\nОтчёт: " + str(journal),
            flush=True,
        )
        return 0
    except KeyboardInterrupt:
        if active:
            runner.stop(active["container"])
        job["status"] = "interrupted"
        checkpoint()
        raise UpdateError(
            "INTERRUPTED", "Cycle interrupted; rerun the same command to continue", 130
        ) from None
    except UpdateError as error:
        job["status"] = "failed"
        job["error"] = {"code": error.code, "message": str(error)}
        checkpoint()
        raise


def interrupt(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local", action="store_true", help="Run installed CLIs inside worker"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("UPDATE_STATE_DIR", ROOT / "reports/update")),
    )
    parser.add_argument(
        "--invalid-objects",
        choices=("skip", "fail"),
        default=os.environ.get("SYNC_INVALID_OBJECTS", "skip"),
        help="Default skip: publish valid objects and report excluded ones",
    )
    parser.add_argument(
        "--max-errors",
        type=int,
        default=0,
        help="Explicit sync error allowance for --invalid-objects fail (default 0)",
    )
    args = parser.parse_args()
    if args.max_errors < 0:
        parser.error("--max-errors must be nonnegative")
    if args.invalid_objects not in {"skip", "fail"}:
        parser.error("SYNC_INVALID_OBJECTS must be skip or fail")
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    signal.signal(signal.SIGTERM, interrupt)
    with os.fdopen(
        os.open(directory / "update.lock", os.O_RDWR | os.O_CREAT, 0o600), "w"
    ) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                "Цикл обновления уже запущен; второй запуск пропущен.", file=sys.stderr
            )
            return 75
        try:
            runner = LocalWorker(directory) if args.local else Compose(ROOT)
            return cycle(runner, directory, args.max_errors, args.invalid_objects)
        except UpdateError as error:
            print(error.code + ": " + str(error), file=sys.stderr)
            return error.exit_code
        except (OSError, ValueError, KeyError, TypeError):
            print(
                "UPDATE_FAILED: проверьте Docker, .env и журнал reports/update/latest.json",
                file=sys.stderr,
            )
            return 1


if __name__ == "__main__":
    sys.exit(main())
