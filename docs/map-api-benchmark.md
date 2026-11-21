# FastAPI/PostGIS baseline

Measured 2026-10-04 through the public HTTP endpoints with
[`benchmark_map_api.py`](../scripts/benchmark_map_api.py). This is an initial
baseline, not a latency target.

## Environment and data

- Apple M1 Max host, Python 3.12.2, one Uvicorn worker, API pool size 10.
- Disposable Postgres 16 / PostGIS 3.5 container (`linux/amd64` image on the
  arm64 host).
- The existing read-only synchronizer published the documented test snapshot
  for `userside/mapId=3`: 7,394 features and 20,730 vertices. The test import
  explicitly used the documented `--max-errors 163`; it recorded 163 source
  issues. The API returned 7,386 visible features and 20,627 vertices; 8
  features were hidden. No production database was modified.
- Whole-network request: `bbox=[-180,-90,180,90]`, zoom 24, all three object
  types, no thematic restriction. Small-area request:
  `bbox=[36.9,54.9,37.1,55.1]` with the same filters.
- Each latency run included a fresh preload and every batch in order. Five
  sequential runs were followed by four simultaneous clients. p95 uses linear
  interpolation over the sample. RSS was sampled from the Uvicorn process.
- The HTTP process used the documented restricted `map_api` role. Direct
  attempts to update `features` and `datasets` with that role were denied.

## HTTP measurements

| Selection | Objects / vertices | Batches | Preload p50 / p95 | First batch p50 / p95 | Full load p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Whole network | 7,386 / 20,627 | 74 | 531 / 603 ms | 11.9 / 13.5 ms | 1,241 / 1,318 ms |
| Small area | 15 / 49 | 1 | 11.0 / 14.2 ms | 6.0 / 6.9 ms | 16.1 / 21.1 ms |

The whole-network batch response bodies totalled 2,453,243 bytes. Gzip level 6
applied to each complete batch response reduced the combined payload to
334,239 bytes (86.4% smaller); the application does not yet enable HTTP gzip
middleware. The small-area response was 5,251 bytes before compression and
1,058 bytes after compression. Peak Uvicorn RSS during these runs was 73,664
KiB.

With four simultaneous whole-network clients, all four finished in 2,686 ms;
per-client full-load p50/p95 was 2,594 / 2,648 ms. Four simultaneous small-area
clients finished in 27.6 ms; per-client p50/p95 was 26.0 / 26.4 ms.

## Spatial query plans

Both plans used the same provider, visibility, zoom, bbox-intersection, and
stable-order filters as preload. The small area used the GiST spatial index;
the whole-network query used the dataset primary-key index.

| Bbox | Plan | Rows | Shared buffers | Planning | Execution |
| --- | --- | ---: | ---: | ---: | ---: |
| `[36.9,54.9,37.1,55.1]` | Bitmap Index Scan on `features_geom_gix`, then Bitmap Heap Scan | 15 | 6 | 24.312 ms | 7.256 ms |
| `[-180,-90,180,90]` | Index Scan using `features_pkey` on `dataset_id` | 7,386 | 547 | 0.221 ms | 5.719 ms |

Plans were captured with `EXPLAIN (ANALYZE, BUFFERS)` against the disposable
snapshot. Results depend on this test hardware, emulated container architecture,
cache state, and the small 7,394-feature dataset. Re-run on the target PostGIS
host and with representative parallel clients before setting production limits
or latency goals.
