import pytest

from map_api.batching import BatchPacker


def test_batch_packer_respects_object_and_vertex_budgets_in_order():
    packer = BatchPacker(max_objects=2, max_vertices=5)
    completed = []
    for item in (
        {"source_id": 1, "vertex_count": 2},
        {"source_id": 2, "vertex_count": 3},
        {"source_id": 3, "vertex_count": 1},
    ):
        completed.extend(packer.push(item))
    completed.append(packer.finish())
    assert [[item["source_id"] for item in batch.items] for batch in completed] == [
        [1, 2],
        [3],
    ]


def test_single_heavy_object_gets_oversized_batch():
    packer = BatchPacker(max_objects=100, max_vertices=5)
    batches = packer.push({"source_id": 4, "vertex_count": 8})
    assert len(batches) == 1
    assert batches[0].object_count == 1
    assert batches[0].vertex_count == 8
    assert batches[0].oversized
    assert packer.finish() is None


def test_empty_and_invalid_batch_budgets():
    assert BatchPacker().finish() is None
    with pytest.raises(ValueError):
        BatchPacker(max_objects=0)
