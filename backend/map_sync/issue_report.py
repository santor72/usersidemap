"""Durable object diagnostics and per-run JSON/Markdown reports."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import SyncError


def source_record(conn, run_id, kind, object_id):
    row = conn.execute(
        "SELECT data FROM map_data.source_records WHERE run_id=%s AND kind=%s AND source_id=%s",
        (run_id, kind, object_id),
    ).fetchone()
    return row["data"] if row else {}


def object_text(conn, run_id, kind, object_id):
    row = source_record(conn, run_id, kind, object_id)
    if kind == "cable":
        start = source_record(conn, run_id, "node", row.get("uzelcode1"))
        if row.get("building2_id"):
            end = source_record(conn, run_id, "building", row["building2_id"])
            end_name = end.get("title") or f"Здание #{row['building2_id']}"
        else:
            end = source_record(conn, run_id, "node", row.get("uzelcode2"))
            end_name = end.get("nazv") or f"Объект #{row.get('uzelcode2')}"
        start_name = start.get("nazv") or f"Объект #{row.get('uzelcode1')}"
        title = f"{start_name} → {end_name}" if row else f"Кабель #{object_id}"
    else:
        title = row.get("nazv") or row.get("title") or f"{kind} #{object_id}"
    return title, row.get("opis") or row.get("description") or ""


def report_cell(value):
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def save_issue_report(conn, run_id, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    run = conn.execute(
        "SELECT id,source_system,map_id,status,started_at,finished_at,error_code,stats FROM map_data.sync_runs WHERE id=%s",
        (run_id,),
    ).fetchone()
    if run is None:
        raise SyncError("Synchronization run not found")
    rows = conn.execute(
        """SELECT object_kind AS type,object_id AS id,object_title AS name,
        object_description AS description,severity,code,detail AS reason,
        CASE WHEN source_kind='cable_group' AND object_kind='cable' THEN source_id END AS group_id
        FROM map_data.sync_issues i WHERE run_id=%s ORDER BY object_kind,object_id,i.id""",
        (run_id,),
    ).fetchall()
    metadata = {**run, "issue_count": len(rows)}
    paths = {suffix: directory / f"sync-{run_id}.{suffix}" for suffix in ("json", "md")}
    temporary = []
    try:
        for suffix, destination in paths.items():
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, prefix=".sync-report-", delete=False
            ) as handle:
                temporary.append(Path(handle.name))
                if suffix == "json":
                    json.dump(
                        {**metadata, "objects": rows},
                        handle,
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                    handle.write("\n")
                else:
                    handle.write(f"# Отчёт синхронизации {run_id}\n\n")
                    handle.write(
                        f"Статус: {run['status']}. Карта: {run['map_id']}. Записей: {len(rows)}.\n\n"
                    )
                    if run["error_code"]:
                        handle.write(f"Ошибка запуска: {report_cell(run['error_code'])}.\n\n")
                    handle.write(
                        "| Тип | ID | Название | Описание | Уровень | Причина ошибки | Трасса |\n"
                    )
                    handle.write("|---|---|---|---|---|---|---|\n")
                    for row in rows:
                        cells = [
                            row["type"],
                            row["id"],
                            row["name"],
                            row["description"],
                            row["severity"],
                            f"{row['code']}: {row['reason']}",
                            row["group_id"],
                        ]
                        handle.write(
                            "| " + " | ".join(report_cell(value) for value in cells) + " |\n"
                        )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, destination)
        return {suffix: str(path.resolve()) for suffix, path in paths.items()}
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
