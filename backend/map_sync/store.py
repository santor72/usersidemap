from functools import lru_cache


class SnapshotStore:
    """Bounded caches over the staged source snapshot, never over the live source."""

    def __init__(self, conn, run_id):
        self.conn = conn
        self.run_id = run_id
        self.get = lru_cache(maxsize=2048)(self._get)
        self.children = lru_cache(maxsize=512)(self._children)

    def _get(self, kind, source_id):
        row = self.conn.execute(
            """SELECT data FROM map_data.source_records
            WHERE run_id=%s AND kind=%s AND source_id=%s""",
            (self.run_id, kind, source_id),
        ).fetchone()
        return row["data"] if row else None

    def _children(self, kind, parent_id):
        return [
            r["data"]
            for r in self.conn.execute(
                """SELECT data
            FROM map_data.source_records WHERE run_id=%s AND kind=%s AND parent_id=%s
            ORDER BY position NULLS FIRST,source_id""",
                (self.run_id, kind, parent_id),
            )
        ]

    def batches(self, kind, size):
        last = -1
        while rows := self.conn.execute(
            """SELECT source_id,data FROM map_data.source_records
            WHERE run_id=%s AND kind=%s AND source_id>%s ORDER BY source_id LIMIT %s""",
            (self.run_id, kind, last, size),
        ).fetchall():
            yield [r["data"] for r in rows]
            last = rows[-1]["source_id"]

    def is_plan(self, map_id):
        row = self.get("map", map_id)
        return bool(row and row["typemap"] == 1)

    def marks(self, source_kind, source_id):
        object_type = 1 if source_kind == "node" else 2
        return [
            r["mark_id"]
            for r in self.children("mark", source_id)
            if r["object_type"] == object_type
        ]

    def settings(self):
        result = {}
        for batch in self.batches("setting", 500):
            for row in batch:
                result.setdefault(row["param"], row)
        return result
