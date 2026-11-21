# Полная raster-сборка: этап 5

2026-10-08. `map-tiles raster --dataset <UUID>` независимо от vector собирает
полный sparse prebake WebP 1x/@2x. Тот же явный ready dataset задаётся обеим
командам. Staging остаётся validated, active не меняется. Publish и файловый
cleanup/rollback реализованы отдельно: [этап 6](cable-tiles-publication.md).

## Запуск и конфигурация

```sh
uv sync --project backend --extra tiles
backend/.venv/bin/map-tiles --env .env raster --dataset <UUID>
```

Worker получает `MAP_TILES_RASTER_DATABASE_URL` с ролью `map_tiles_raster`.
Нельзя использовать API/source DSN. Deployment init включает LOGIN этой роли
при `MAP_TILES_RASTER_DB_PASSWORD`; остальные settings общие с
[vector CLI](cable-tiles-vector-pipeline.md). Добавлен
`TILES_RASTER_PREBAKE_MAX`, default 15. DSN выбирается по subcommand, без fallback
на owner, API или worker другого формата.

Raster source tileSize=256 запрашивает XYZ z=q+1 при map zoom q. Поэтому native
maxzoom ≥15 необходим для полного состава кабелей logical z14, включая короткие.
Конфигурация допускает 15…min(24,displayMaxzoom+1). Default displayMaxzoom=18.
Planner и tile_inputs применяют min_zoom<=z-1, geometry tolerance берётся по XYZ z.
Vector и raster фиксируют общий canon table до max(vectorMaxzoom,rasterMaxzoom),
одинаковый tolerance hash и общий hash cable predicate/style/types.

## Worker, manifest и проверка

Общий orchestration фиксирует BuildSpec, pin, session lease и heartbeat.
Raster runner `map_tiles.raster_worker` использует spawn ProcessPoolExecutor:
по умолчанию 2 процесса; каждый имеет свою DB connection и Cairo/Pillow.
После пакета ≤concurrency×64 задач процессы завершаются и pool создаётся заново.
Это ограничивает накопление RSS и не использует нестабильный recycle
max_tasks_per_child, отвергнутый на этапе прототипа.

Каждый XYZ записывается сразу в обеих плотностях, независимо кодированным
lossless WebP; Cairo strokes solid, non-backbone снизу. Пути:
`<mapId>/raster/.staging/<version>/<z>/<x>/<y>.webp` и `<y>@2x.webp`.
Даши и hit-test/filter raster не поддерживает согласно контракту. Геометрия,
buffer, порядок и alpha conversion сохранены из принятого renderer.

Manifest содержит format= raster, encoding=webp, prebakeMaxzoom, tileSize=256,
densities=[1,2], shared profile/hash, bounds и native coverage. Запись XYZ:
status=prebaked, objectIds входа, files для обеих плотностей с path/bytes/SHA256.
Выше native maxzoom — диапазон client overzoom без файлов. Coverage после
публикации не дописывается.

Validate требует каждую плотность для каждого запланированного XYZ, checksum,
успешное независимое Pillow-декодирование WEBP и размеры 256/512, полный zoom
range, отсутствие дополнительных файлов/симлинков и совпадение disk manifest.
Нет записи внутри native — empty, отсутствие ожидаемого файла — missing,
непригодный для publish. ObjectIds являются диагностикой входа: они не
декодируются из raster pixels. Их состав сверяется с vector и SQL none отдельно.

Ожидание futures каждые 0.2 s проверяет heartbeat/lease. Ошибка/отмена устанавливает
shared cancellation event, отменяет queued tasks, дожидается текущих процессов,
закрывает pool и записывает failed build. Автоматический cleanup удаляет staging
и оставляет tombstone; при ошибке GC файлы сохраняются с cleanup=error. DB statement_timeout
30 s ограничивает ожидание текущего запроса. Safe TileError поддерживает pickle;
worker передаёт только код и класс ошибки без connection secrets. Pending cleanup
raster препятствует только новой raster-сборке; vector остаётся независимым.

Zoom report содержит counts/bytes/seconds/peakWorkerRssMiB; full report отдельно
содержит validationSeconds. Native files после validated готовы к этапу publish.

## Доказательства

Четыре raster integration gates на disposable PostGIS:

- полный z1–15 на fixtures, обе плотности, native input union равен vector14,
  short cable/group/layers и совпадение hashes;
- missing @2x, checksum corruption и corrupt WebP с корректно подменённым hash
  не проходят validate; dimensions/alpha и отсутствие оставшихся процессов;
- отмена/pending блокирует raster, но полный vector того же dataset succeeds;
- реальная ошибка Cairo в spawned process передаётся безопасно, pool завершается,
  build failed/RASTER_WORKER_FAILED; проверены native minimum и pickle.

Рендерные gates этапа 2 дополнительно проверяют непрерывность former-dashed,
straight alpha, backbone, short cable на native/overzoom, швы и логическую ширину
3px при DPR1/DPR2. Production pipeline использует тот же renderer. Pixel-diff
с GeoJSON не применяется.

Полный snapshot (7113 features) импортирован в disposable PostGIS. Настоящие
vector/raster CLI работали с одним UUID, отдельными LOGIN ролями. Raster запущен
в Docker worker с --memory=1g, --cpus=2, concurrency=2; vector работал параллельно.
Raster cycle render+validate = 83.48s, validate = 9.42s, peak одного Linux worker
84.39 MiB. Это ещё не полный эксплуатационный цикл publish/GC, поэтому grace
period будет выбран на этапе 10. Shared canon hash соответствует этапу 2.

Все WebP повторно проверены с диска; native raster15/vector14 union равен SQL none
(3266 IDs), hashes совпали, обе версии validated, active пуст. Counts, bytes по
плотностям и оценка `2 retained + staging` для обоих форматов:
`reports/tiles/raster-cli-audit.json`; zoom report — `raster-cli-full.jsonl`;
manifest — `raster-cli-manifest.json`. Размер включает обе плотности и manifests;
это оценка по измеренному полному выпуску, фактическое удержание/GC проверит этап 6.

Кадр из Docker prebake: [native15 @2x](assets/cable-tiles-prototype/raster-pipeline-native15-2x.png),
XYZ 15/19823/10293, 77 input IDs; при просмотре видны непрерывные solid линии
и прозрачный фон. Измерен также allocated disk через du -sk, а не только payload bytes.

108 backend tests passed, 0 skipped, 73.41s; Ruff passed. Этап 5 принят.
Regression suite и статус приёмки записаны в `cable-tiles-validation.md`.
Рабочие данные не менялись.
