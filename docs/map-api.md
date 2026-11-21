# Map API v1

The API reads immutable published datasets from PostGIS and does not connect to
Userside. Start it with `uv run --project backend map-api`; set
`MAP_API_DATABASE_URL` to the restricted API database role (it falls back to
`MAP_DB_URL` for local development). Bind to a trusted network until
authentication is added. CORS is controlled by `MAP_API_CORS_ORIGINS`.

## Layers

`GET /api/map/layers?mapId=3` returns the active version, visible object types,
and thematic layers with object counts.

## Two-phase loading

`POST /api/map/objects/preload` accepts:

```json
{"mapId":3,"bbox":[-123,37,-122,38],"zoom":15,"objectTypes":["cable_line","node"],"layerIds":[12]}
```

Types are `cable_line`, `node`, and `coupler`; an empty list selects nothing.
The bbox can cross the antimeridian by setting west greater than east. Empty
`layerIds` means all thematic layers. Visibility, zoom, and intersection with
the complete geometry are applied on the server. Features are never clipped.

The response includes `queryId`, pinned `datasetVersion`, `expiresAt`, object
and vertex totals, type counts, and compact batch descriptors. A heavy object
may occupy a batch alone and is marked `oversized`. Query limits and batch
budgets are server settings. Defaults are 100 objects / 5000 vertices per
batch, 100000 objects / 2000000 vertices per query, 100000 vertices per
object, and a 10 minute lease.

Fetch one descriptor with `POST /api/map/objects/batch`:

```json
{"queryId":"00000000-0000-0000-0000-000000000000","batchIndex":0}
```

It returns a GeoJSON FeatureCollection with stable `sourceKind:sourceId`
feature IDs, object type, title, and compact style. Retries return the same
batch. `GET /api/map/objects/{sourceKind}/{sourceId}?queryId=...` returns saved
properties, length, links, and thematic memberships for objects in the lease.

Errors use `{"error":{"code":"...","message":"..."}}`. Invalid input is
422, missing query/batch/object is 404, expired retained leases are 410,
missing published data or database are 503, and a preparation deadline returns
503 (`PRELOAD_TIMEOUT`). Resource limits are 413
(`QUERY_LIMIT_EXCEEDED` or `OBJECT_TOO_LARGE`). After administrative cleanup,
an expired query becomes 404; either response tells clients to preload again.

## Health and operations

`GET /health/live` checks the process. `/health/ready` checks PostGIS and the
required schema. `MAP_API_SOURCE_SYSTEM` selects the published source.
`MAP_API_VISIBLE_CABLE_TYPE_IDS` is a comma-separated allowlist of cable type IDs
returned by map catalog counts and preload (default `1`). Cable rows of other types
remain imported in the published dataset and can be enabled later by adding IDs,
for example `1,3`.
`MAP_API_HOST` and `MAP_API_PORT` control the listener (localhost is the
default), and `MAP_API_DATABASE_URL` should point to the restricted runtime
role. Configure `MAP_API_POOL_MIN`, `MAP_API_POOL_MAX`,
`MAP_API_POOL_TIMEOUT`, `MAP_API_STATEMENT_TIMEOUT_MS`,
`MAP_API_LOCK_TIMEOUT_MS`,
`MAP_API_CONNECT_TIMEOUT_SECONDS`,
`MAP_API_TTL_SECONDS`, `MAP_API_MAX_OBJECTS`, `MAP_API_MAX_VERTICES`,
`MAP_API_PRELOAD_TIMEOUT_SECONDS`,
`MAP_API_HARD_VERTICES`, `MAP_API_BATCH_OBJECTS`, and
`MAP_API_BATCH_VERTICES` for deployment. Run `map-db cleanup` from one
administrative scheduled job; API workers do not start cleanup timers.

Apply migrations with the administrative role. Grant the API role SELECT on
`map_data.datasets`, `active_datasets`, `features`, `layers`, `feature_layers`,
and `feature_links`, plus SELECT/INSERT/UPDATE/DELETE on `query_leases`,
`query_batches`, `query_items`, and `query_lease_totals`, and USAGE on the
`map_data` schema. Do not grant ownership or write access to published data.

For a role named `map_api`, an administrator can apply these grants after each
migration:

```sql
GRANT USAGE ON SCHEMA map_data TO map_api;
GRANT SELECT ON map_data.datasets, map_data.active_datasets, map_data.features,
  map_data.layers, map_data.feature_layers, map_data.feature_links TO map_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON map_data.query_leases,
  map_data.query_batches, map_data.query_items, map_data.query_lease_totals TO map_api;
GRANT EXECUTE ON FUNCTION map_data.api_pin_active_dataset(text, bigint) TO map_api;
```

This role cannot publish versions or modify feature data. Migrations and the
runtime do not create roles or change grants automatically.

Run the integration suite against a disposable PostGIS named by
`TEST_MAP_DB_URL`:

```sh
TEST_MAP_DB_URL=postgresql://... uv run --project backend pytest backend/tests
```

For a repeatable HTTP measurement, start `map-api` and run
`uv run python scripts/benchmark_map_api.py --base-url http://127.0.0.1:8000`.
The current test-snapshot baseline and query plans are in
[`map-api-benchmark.md`](map-api-benchmark.md).
