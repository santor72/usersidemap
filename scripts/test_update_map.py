"""Orchestration failure/resume gates; no Docker or production DB required."""

import fcntl
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SPEC = importlib.util.spec_from_file_location(
    "update_map", Path(__file__).with_name("update-map.py")
)
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


class FakeCompose:
    def __init__(self, fail=None, mismatch=False, pending=False, excluded=0):
        self.fail, self.mismatch, self.pending = fail, mismatch, pending
        self.calls = []
        self.dataset = str(uuid4())
        self.versions = {}
        self.excluded = excluded

    def scope(self):
        return {"sourceSystem": "userside", "mapId": 3}

    def prepare(self):
        pass

    def remove(self, name):
        pass

    def stop(self, name):
        self.stopped = name

    def execute(self, name, service, args, retry, receive):
        self.calls.append((service, args))
        step = (
            "publish-" + args[2]
            if args[0] == "publish"
            else "cleanup-" + args[2]
            if args[0] == "cleanup"
            else args[0]
        )
        if step == self.fail:
            return 1
        if step == "sync":
            self.dataset = str(uuid4())
            receive(
                {"stage": "start", "datasetId": self.dataset, "runId": str(uuid4())}
            )
            receive(
                {
                    "stage": "published",
                    "datasetId": self.dataset,
                    "runId": str(uuid4()),
                    "token": "do-not-persist",
                    "dsn": "postgresql://secret",
                    "stats": {"excludedObjects": self.excluded},
                }
            )
        elif step in ("vector", "raster"):
            self.versions[step] = str(uuid4())
            receive(
                {
                    "status": "validated",
                    "tileVersion": self.versions[step],
                    "datasetVersion": str(uuid4())
                    if self.mismatch and step == "raster"
                    else args[2],
                    "format": step,
                }
            )
        elif step.startswith("publish-"):
            receive(
                {
                    "published": True,
                    "version": args[4],
                    "datasetVersion": self.dataset,
                    "cleanup": {"cleanupPending": self.pending},
                }
            )
            return 2 if self.pending else 0
        else:
            receive({"cleanupPending": False})
        return 0


def test_success_order_same_snapshot_and_next_cycle(tmp_path):
    runner = FakeCompose()
    assert update.cycle(runner, tmp_path) == 0
    commands = [args[0] for _, args in runner.calls]
    assert commands == [
        "sync",
        "vector",
        "raster",
        "publish",
        "publish",
        "cleanup",
        "cleanup",
    ]
    assert runner.calls[1][1][2] == runner.calls[2][1][2] == runner.dataset
    report = json.loads((tmp_path / "latest.json").read_text())
    assert report["status"] == "completed" and report["datasetId"] == runner.dataset
    assert "do-not-persist" not in (tmp_path / "latest.json").read_text()
    assert "postgresql://" not in (tmp_path / "latest.json").read_text()
    old = report["id"]
    runner.calls.clear()
    assert update.cycle(runner, tmp_path) == 0
    assert runner.calls[0][1] == [
        "sync",
        "--max-errors",
        "0",
        "--invalid-objects",
        "skip",
    ]
    assert json.loads((tmp_path / "latest.json").read_text())["id"] != old


@pytest.mark.parametrize("failed_step", ["sync", "raster", "publish-raster"])
def test_retry_resumes_same_cycle_without_repeating_success(tmp_path, failed_step):
    runner = FakeCompose(fail=failed_step)
    with pytest.raises(update.UpdateError):
        update.cycle(runner, tmp_path)
    report = json.loads((tmp_path / "latest.json").read_text())
    assert report["status"] == "failed"
    if failed_step == "raster":
        assert not any(args[0] == "publish" for _, args in runner.calls)
    previous = runner.dataset
    runner.calls.clear()
    runner.fail = None
    assert update.cycle(runner, tmp_path) == 0
    final = json.loads((tmp_path / "latest.json").read_text())
    assert final["id"] == report["id"]
    if failed_step != "sync":
        assert final["datasetId"] == previous
        assert not any(args[0] == "sync" for _, args in runner.calls)
        assert not any(args[0] == "vector" for _, args in runner.calls)
    if failed_step == "publish-raster":
        assert runner.calls[0][1][0] == "publish" and runner.calls[0][1][2] == "raster"


def test_excluded_objects_do_not_block_bakes_and_are_counted_in_checkpoint(tmp_path):
    runner = FakeCompose(excluded=151)
    assert update.cycle(runner, tmp_path) == 0
    journal = json.loads((tmp_path / "latest.json").read_text())
    assert journal["invalidObjects"] == "skip"
    assert journal["steps"]["sync"]["result"]["excludedObjects"] == 151
    assert journal["steps"]["raster"]["status"] == "succeeded"
    assert journal["steps"]["publish-raster"]["status"] == "succeeded"


def test_worker_snapshot_mismatch_stops_before_publication(tmp_path):
    runner = FakeCompose(mismatch=True)
    with pytest.raises(update.UpdateError, match="snapshot"):
        update.cycle(runner, tmp_path)
    assert not any(args[0] == "publish" for _, args in runner.calls)


def test_published_with_cleanup_exit_two_is_not_republished(tmp_path):
    runner = FakeCompose(pending=True)
    assert update.cycle(runner, tmp_path) == 0
    report = json.loads((tmp_path / "latest.json").read_text())
    assert report["steps"]["publish-vector"]["exitCode"] == 2
    assert report["steps"]["publish-vector"]["status"] == "succeeded"
    assert report["steps"]["cleanup-vector"]["status"] == "succeeded"


def test_different_map_cannot_resume_pending_job(tmp_path):
    runner = FakeCompose(fail="raster")
    with pytest.raises(update.UpdateError):
        update.cycle(runner, tmp_path)
    runner.scope = lambda: {"sourceSystem": "other", "mapId": 4}
    with pytest.raises(update.UpdateError, match="source/map"):
        update.cycle(runner, tmp_path)


def test_parallel_launch_is_rejected_without_running_docker(tmp_path):
    with (tmp_path / "update.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("update-map.py")),
                "--state-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd="/tmp",
        )
        assert result.returncode == 75
        assert "уже запущен" in result.stderr
        assert not (tmp_path / "latest.json").exists()


def test_sync_start_uuid_is_not_accepted_as_published():
    assert (
        update.result_for("sync", {"stage": "start", "datasetId": str(uuid4())}) is None
    )


def test_explicit_error_allowance_retries_only_failed_sync(tmp_path):
    runner = FakeCompose(fail="sync")
    with pytest.raises(update.UpdateError):
        update.cycle(runner, tmp_path)
    runner.fail = None
    runner.calls.clear()
    assert update.cycle(runner, tmp_path, max_errors=151, invalid_objects="fail") == 0
    assert runner.calls[0][1] == [
        "sync",
        "--max-errors",
        "151",
        "--invalid-objects",
        "fail",
    ]
    assert json.loads((tmp_path / "latest.json").read_text())["maxErrors"] == 151
    assert (tmp_path / "latest.json").stat().st_mode & 0o077 == 0


def test_cleanup_failure_resumes_cleanup_without_new_data_or_republish(tmp_path):
    runner = FakeCompose(fail="cleanup-vector")
    with pytest.raises(update.UpdateError):
        update.cycle(runner, tmp_path)
    runner.fail = None
    runner.calls.clear()
    assert update.cycle(runner, tmp_path) == 0
    assert [args[0] for _, args in runner.calls] == ["cleanup", "cleanup"]


def test_existing_exited_container_result_is_recovered_without_new_run(monkeypatch):
    runner = update.Compose(Path("/example"))
    monkeypatch.setattr(runner, "inspect", lambda _: {"Running": False, "ExitCode": 0})
    commands = []
    received = []

    class Process:
        stdout = io.StringIO(json.dumps({"published": True}) + "\n")

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        update.subprocess,
        "Popen",
        lambda command, **_: commands.append(command) or Process(),
    )
    assert (
        runner.execute("retained", "tiles-vector", ["publish"], True, received.append)
        == 0
    )
    assert commands == [["docker", "logs", "--follow", "retained"]]
    assert received == [{"published": True}]


def test_logs_exit_zero_does_not_hide_failed_container_exit(monkeypatch):
    runner = update.Compose(Path("/example"))
    monkeypatch.setattr(runner, "inspect", lambda _: {"Running": False, "ExitCode": 1})
    received = []

    class Process:
        stdout = io.StringIO(
            "Validation failed: object errors exceed max-errors; inspect sync_issues\n"
        )

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(update.subprocess, "Popen", lambda *_, **__: Process())
    assert runner.execute("failed", "jobs", ["sync"], False, received.append) == 1
    assert received == [{"event": "command_error", "code": "SYNC_VALIDATION_FAILED"}]


def test_local_worker_recovers_committed_result_without_repeating_cli(
    tmp_path, monkeypatch
):
    runner = update.LocalWorker(tmp_path)
    counter = tmp_path / "calls"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import json; "
            f"Path({str(counter)!r}).write_text('once'); "
            "print(json.dumps({'cleanupPending':False}))"
        ),
    ]
    monkeypatch.setattr(runner, "command", lambda *_: (command, {}))
    received = []
    assert runner.execute("step", "tiles-vector", [], False, received.append) == 0
    assert received == [{"cleanupPending": False}]
    assert runner.paths("step")[1].read_text() == "0"
    monkeypatch.setattr(
        runner, "command", lambda *_: pytest.fail("must recover receipt")
    )
    received.clear()
    assert runner.execute("step", "tiles-vector", [], True, received.append) == 0
    assert received == [{"cleanupPending": False}]
    assert runner.paths("step")[0].stat().st_mode & 0o077 == 0


def test_local_worker_retries_failed_cli_and_discards_incomplete_log(
    tmp_path, monkeypatch
):
    runner = update.LocalWorker(tmp_path)
    monkeypatch.setattr(
        runner,
        "command",
        lambda *_: ([sys.executable, "-c", "raise SystemExit(1)"], {}),
    )
    assert runner.execute("step", "jobs", [], False, lambda _: None) == 1
    monkeypatch.setattr(
        runner,
        "command",
        lambda *_: ([sys.executable, "-c", "print('{\"cleanupPending\":false}')"], {}),
    )
    received = []
    assert runner.execute("step", "jobs", [], True, received.append) == 0
    runner.paths("step")[1].unlink()
    runner.paths("step")[0].write_text('{"cleanupPending":true}\n')
    received.clear()
    assert runner.execute("step", "jobs", [], True, received.append) == 0
    assert received == [{"cleanupPending": False}]


def test_local_subprocess_credentials_are_scoped_to_role(tmp_path, monkeypatch):
    for key in (
        "MAP_DB_PASSWORD",
        "MAP_TILES_VECTOR_DB_PASSWORD",
        "MAP_TILES_RASTER_DB_PASSWORD",
        "US_DB_PASS",
        "MAP_API_DB_PASSWORD",
        "TILES_S3_ACCESS_KEY",
        "TILES_S3_SECRET_KEY",
        "TILES_S3_API_SECRET_KEY",
    ):
        monkeypatch.setenv(key, "test-secret/@")
    runner = update.LocalWorker(tmp_path)
    for format in ("vector", "raster"):
        cmd, env = runner.command("tiles-" + format, [format])
        assert cmd == ["map-tiles", format]
        assert not any(k.startswith(("US_DB_", "MAP_DB_", "MAP_API_")) for k in env)
        keys = [k for k in env if k.startswith("MAP_TILES_")]
        assert keys == ["MAP_TILES_" + format.upper() + "_DATABASE_URL"]
        assert "test-secret%2F%40" in env[keys[0]]
        assert env["TILES_S3_SECRET_KEY"] == "test-secret/@"
        assert "TILES_S3_API_SECRET_KEY" not in env
    cmd, env = runner.command("jobs", ["sync"])
    assert cmd == ["map-db", "sync"]
    assert env["US_DB_PASS"] == "test-secret/@"
    assert not any(k.startswith(("MAP_TILES_", "TILES_S3_")) for k in env)
