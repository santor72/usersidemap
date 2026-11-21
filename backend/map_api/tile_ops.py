"""Internal Prometheus scrape: durable lifecycle state, never signed URLs or tokens."""

import asyncio

from fastapi.responses import Response

SQL = """SELECT a.source_system,a.map_id,f.format,
 CASE WHEN v.id IS NULL OR v.dataset_version<>a.dataset_id THEN 1 ELSE 0 END AS behind,
 CASE WHEN v.id IS NULL OR v.dataset_version<>a.dataset_id THEN
 GREATEST(0,extract(epoch FROM clock_timestamp()-COALESCE(d.published_at,d.created_at))) ELSE 0 END AS lag,
 (SELECT count(*) FROM map_data.tile_versions r WHERE r.source_system=a.source_system
   AND r.map_id=a.map_id AND r.format=f.format AND r.status='ready') AS retained,
 (SELECT count(*) FROM map_data.tile_versions r WHERE r.source_system=a.source_system
   AND r.map_id=a.map_id AND r.format=f.format AND r.cleanup_status IN ('pending','error')) AS cleanup,
 COALESCE(b.status IN ('failed','interrupted'),false)::int AS failed,
 COALESCE(extract(epoch FROM COALESCE(b.finished_at,clock_timestamp())-b.started_at),0) AS build_seconds
 FROM map_data.active_datasets a JOIN map_data.datasets d ON d.id=a.dataset_id
 CROSS JOIN (VALUES ('vector'),('raster')) AS f(format)
 LEFT JOIN map_data.active_tile_versions p ON p.source_system=a.source_system
   AND p.map_id=a.map_id AND p.format=f.format
 LEFT JOIN map_data.tile_versions v ON v.id=p.active_version
 LEFT JOIN LATERAL (SELECT status,started_at,finished_at FROM map_data.tile_builds t
   WHERE t.source_system=a.source_system AND t.map_id=a.map_id AND t.format=f.format
   ORDER BY started_at DESC LIMIT 1) b ON true
 WHERE a.source_system=%s ORDER BY a.map_id,f.format"""


def install_tile_ops(app, settings, set_timeouts):
    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        async with app.state.pool.connection() as conn:
            await set_timeouts(conn)
            rows = await (await conn.execute(SQL, (app.state.tile_config.source,))).fetchall()
        lines = []

        def emit(name, value, labels=""):
            lines.append(f"map_tiles_{name}{labels} {value}")

        for name in ("status_cache_hits", "status_cache_misses", "status_cache_errors"):
            lines.append(f"# TYPE map_tiles_{name}_total counter")

        def label(value):
            return (
                '"'
                + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                + '"'
            )

        emit("lag_grace_seconds", app.state.tile_config.lag_grace_seconds)
        for row in rows:
            labels = (
                "{"
                + ",".join(
                    f"{key}={label(row[key])}" for key in ("source_system", "map_id", "format")
                )
                + "}"
            )
            for metric, column in (
                ("behind", "behind"),
                ("lag_seconds", "lag"),
                ("retained_versions", "retained"),
                ("cleanup_pending", "cleanup"),
                ("latest_build_failed", "failed"),
                ("latest_build_seconds", "build_seconds"),
            ):
                emit(metric, row[column], labels)
        cache = app.state.tile_status_cache
        emit("status_cache_hits_total", cache.hits)
        emit("status_cache_misses_total", cache.misses)
        emit("status_cache_errors_total", cache.errors)
        emit("redis_enabled", int(cache.client is not None))
        up = 0
        if cache.client:
            try:
                up = int(bool(await asyncio.wait_for(cache.client.ping(), timeout=0.3)))
            except Exception:  # noqa: BLE001 -- Redis is optional, DB remains authoritative
                up = 0
        emit("redis_up", up)
        return Response(
            "\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4",
            headers={"Cache-Control": "no-store"},
        )
