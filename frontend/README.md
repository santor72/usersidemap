# Userside map frontend

Nuxt 4 hosts the demonstration page and two local Nuxt modules. `map-data` owns
the FastAPI protocol, batch queue, query/version state and canonical GeoJSON
objects. `cartography` owns the controller and the MapLibre adapter; the page
uses only the controller interface. MapLibre is created in a client-only
component. The browser calls `/api/map`; Nitro forwards that path to FastAPI on
the Compose network.

## Run

From the repository root, copy `.env.example` to `.env`, configure the two
destination database passwords and the read-only Userside connection, then run:

```sh
docker compose up -d --build --wait
docker compose run --rm jobs sync
```

Compose logs invalid objects in the synchronization report and publishes valid
objects. Open `http://localhost:3000` by default. To run the Nuxt dev
server on the host, use `cd frontend && npm ci &&
MAP_API_INTERNAL_URL=http://127.0.0.1:8006 npm run dev` when Compose publishes
the API on its default host port. If `MAP_API_PORT` differs, use that port.
The dev server forwards `/api/map` through Nitro to this server-only URL.

The initial map view is configurable with `NUXT_PUBLIC_MAP_LONGITUDE`,
`NUXT_PUBLIC_MAP_LATITUDE`, and `NUXT_PUBLIC_MAP_ZOOM`. The default view
(`37.664, 55.76005`, zoom 15) is chosen from the published snapshot so cables,
nodes, and couplers are present in the initial viewport. `NUXT_PUBLIC_MAP_ID`
selects the published map. The 2GIS tile URL and attribution have separate
`NUXT_PUBLIC_MAP_TILES_URL` and `NUXT_PUBLIC_MAP_ATTRIBUTION` settings.
`MAP_API_INTERNAL_URL` is server-only; it is never included in browser config.
Use Node.js 22.19+, 24.11+, or 26+ for local Nuxt commands.

## Extend object types

1. Add the type and its source kinds to `backend/map_api/providers.py` and make
   the sync builder publish its geometry and properties.
2. Add a title to `app/map-types.ts`. The current MapLibre adapter treats types
   other than `cable_line` as point layers. Nodes and couplers use Lucide symbols
   from `runtime/adapters/maplibre/point-symbols.ts`; other types keep backend styles.
3. Update the adapter's type-specific symbol rules if the new geometry needs a
   different renderer. Keep the type ID stable between catalog, API features,
   and `objectTypes`.

The adapter contract and lifecycle are in
[`docs/frontend-extension.md`](../docs/frontend-extension.md). The three
object types are application configuration; thematic layer IDs remain filters
sent separately as `layerIds` by the data client.

The cartography Nuxt plugin provides `$createMapAdapter(container, options)`.
Its default factory lazily loads `MapLibreAdapter`. A different renderer can
implement `MapAdapter` (or extend `MapAdapterTemplate`) and replace this factory
without changing `MapController` or the application panels. `MapCanvas` owns a
`ResizeObserver` and forwards size changes through `adapter.resize()`.

## Point symbols

Nodes use Lucide `network`; couplers use `link-2` (connection). SVGs and Noto Sans
Latin/Cyrillic label fonts are bundled locally. Backend colors are retained as
icon backgrounds; a yellow ring identifies the selected object.

`NUXT_PUBLIC_MAP_ICON_BASE_SIZE` defaults to 24 CSS pixels at zoom 16. Icons
interpolate from half that size at zoom 10 to 1.25 times that size at zoom 19,
with those sizes clamped beyond the endpoints. `NUXT_PUBLIC_MAP_LABEL_MIN_ZOOM`
defaults to 16; below that zoom labels are hidden. MapLibre suppresses overlapping
labels while keeping icons visible. The settings can be changed in the panel.
`NUXT_PUBLIC_MAP_ICON_MIN_ZOOM` (default 15, range 0–22) controls when node
and coupler icons appear. The panel slider changes both loading and rendering
immediately, including below zoom 15, without rebuilding snapshots or tiles.
Labels appear only when both the icon and label thresholds are reached.
These settings are saved locally in the browser; saved values override `.env`
defaults. Existing saved settings without an icon threshold use its `.env` value.
To extend point symbols, add an SVG import
and object-type entry in `pointIcons`; the data-loading protocol is unchanged.

## Cable tiles (stages 8–9)

Stages 8–9 add none/vector/raster to the Nuxt controller and UI. Set
`NUXT_PUBLIC_CABLE_TILES=vector` and a server-only `NUXT_MAP_TILES_TOKEN` with
metadata/details scopes. Change the deployment default with a process restart;
`CABLE_TILES` is a dev/build fallback. Metadata and details use `/api/maps` through
Nitro; XYZ use the origin templates directly. Points are pinned to the vector
release dataset, while none retains the unpinned object protocol.

The configuration, cold start/lag behavior, evidence and disposable browser
fixture are described in [cable-tiles-frontend.md](../docs/cable-tiles-frontend.md).
Raster uses 256px WebP and @2x at DPR ≥1.5. Catalog refreshes on focus, configured
poll, 410 and before HMAC expiry even with poll=0. Compose now wires the flag,
private token, separate workers, Redis and the API's read-only tile volume.

Production Compose и GIS API: [эксплуатационный гайд](../docs/cable-tiles-operations.md).
