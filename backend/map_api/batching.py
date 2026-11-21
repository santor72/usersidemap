from dataclasses import dataclass


@dataclass(frozen=True)
class PackedBatch:
    items: list[dict]
    vertex_count: int
    oversized: bool

    @property
    def object_count(self):
        return len(self.items)


class BatchPacker:
    """Keeps only one bounded batch of metadata in memory."""

    def __init__(self, max_objects=100, max_vertices=5000):
        if max_objects < 1 or max_vertices < 1:
            raise ValueError("Batch budgets must be positive")
        self.max_objects = max_objects
        self.max_vertices = max_vertices
        self.items = []
        self.vertex_count = 0

    def push(self, item):
        count = item["vertex_count"]
        completed = None
        if self.items and (
            len(self.items) >= self.max_objects or self.vertex_count + count > self.max_vertices
        ):
            completed = self._take()
        self.items.append(item)
        self.vertex_count += count
        if count > self.max_vertices:
            oversized = self._take()
            return [completed, oversized] if completed else [oversized]
        return [completed] if completed else []

    def finish(self):
        return self._take() if self.items else None

    def _take(self):
        batch = PackedBatch(
            items=self.items,
            vertex_count=self.vertex_count,
            oversized=self.vertex_count > self.max_vertices,
        )
        self.items = []
        self.vertex_count = 0
        return batch
