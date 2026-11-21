# Жизненный цикл тайлов: этап 3

2026-10-08. Модель проверена на отдельном PostgreSQL 16 / PostGIS 3.5 и Redis 7.
Рабочий dataset и active pointers не изменены. Файловые publish, rollback и GC
реализуются на этапе 6; SQL-модели в тестах не публикуют файлы.

## Модель БД

Миграция `008_tile_lifecycle.sql` создаёт:

- `tile_builds`: running → succeeded | failed | interrupted; одна running-сборка
  на `(map_id, format)`, UUID выпуска, параметры, heartbeat и безопасный код ошибки.
- `tile_versions`: building → validated → ready → retired → deleted;
  building/validated могут перейти в failed, затем deleted. Правила, hash,
  zoom, geometry profile, datasetVersion и относительные пути фиксируются при
  создании; manifest неизменяем после validated.
- `active_tile_versions`: active + prev на `(source_system, map_id, format)`.
  Отложенные constraints на commit разрешают перестановку указателей и требуют,
  чтобы ready были ровно active/prev, максимум две версии в каждой области.

Composite FK запрещают чужой dataset, формат или карту в указателях. Канонические
пути имеют вид `<mapId>/<format>/.staging/<UUID>` и `<mapId>/<format>/<UUID>`.
Файловый root не хранится в БД и будет проверяться отдельно при работе с диском.

`dataset_version` — постоянный UUID происхождения. Nullable `dataset_id` — FK
для удержания snapshot. После cleanup терминального snapshot FK очищается,
архивный UUID остаётся в tombstone. `cleanup_status` принимает none, pending,
done, error; deleted требует done. Tombstones не воскресают и остаются доступны
для будущего ответа 410. API status cache также сохраняет metadata deleted.

## Блокировки и heartbeat

`TileLock` использует dedicated autocommit connection и session advisory lock
`hashtextextended('map:tiles:<mapId>:<format>', 0)` на весь bake/publish/cleanup.
Vector и raster независимы. Смена backend-сессии не сохраняет lease.
`check()` проверяет настоящий lock через `pg_locks`, а не только флаг Python.

Порядок блокировок:

1. Worker берёт session lock своей карты/формата.
2. В короткой транзакции берёт общий `pg_advisory_xact_lock(79124002)`.
3. Затем берёт dataset/pointer/row locks и фиксирует pin или публикацию.

Sync publish и snapshot cleanup начинают с того же transaction lock, затем
берут свои row/table locks; они не запрашивают session lock worker. Рендер не
держит общий transaction lock. Это сериализует создание pin и удаление snapshot,
не блокируя длительные vector/raster сборки друг другом.

`tiles_pin_dataset` — SECURITY DEFINER с фиксированным search_path, недоступен
PUBLIC. Он проверяет ready, source и map, блокируя snapshot FOR SHARE.
Snapshot cleanup пропускает building/validated/ready версии обоих форматов,
running builds, active dataset и действующие query leases. Дополнительно BEFORE
DELETE trigger запрещает удаление dataset, удерживаемого выпуском/build.

`BuildHeartbeat` работает отдельной autocommit connection, обычно раз в 10 s,
с statement_timeout 5 s. Перед обновлением проверяет advisory lock основного
backend PID. Pipeline должен вызывать `check()` между пакетами и завершать
heartbeat context перед переводом build в terminal. Потеря lock/соединения или
running-записи останавливает heartbeat и возвращает BUILD_HEARTBEAT_FAILED.
Connection factory должна задавать ограниченный connect_timeout.

Recovery разрешён только новому фактическому владельцу session lock. Свежий
heartbeat (по умолчанию до 120 s) препятствует recovery. Stale running build
становится interrupted, building/validated версия — failed + pending cleanup.
Если версия уже ready (сбой после commit публикации, до finish build), build
становится succeeded/published, active сохраняется. Pending/error cleanup
блокирует начало следующей сборки этого формата; файловую очистку добавит этап 6.

## Роли и кэш статуса

`grant_tile_roles` выдаёт `map_tiles_vector` и `map_tiles_raster` SELECT snapshot
и SELECT/INSERT/UPDATE lifecycle-таблиц. RLS разрешает изменения только строк
своего формата. DELETE не выдан. API имеет только SELECT lifecycle-таблиц.
Deployment init создаёт роли, устанавливает LOGIN при наличии
`MAP_TILES_VECTOR_DB_PASSWORD` / `MAP_TILES_RASTER_DB_PASSWORD`, иначе NOLOGIN;
compose-конфигурация worker будет добавлена на этапе 10.

Sync/async `VersionStatusCache` хранит малую metadata версии по ключу
`map:tiles:status:v1:<UUID>`, TTL 30 s. Miss → DB → SETEX, неизвестный UUID не
кэшируется; Redis down/невалидный JSON → DB. Redis connect/socket timeout 0.2 s,
без повторов. В lifecycle invalidation вызывается после commit; будущие
publish/rollback/retire должны вызывать её для всех затронутых UUID.

Гонка возможна: DB miss считал ready до retire, SETEX пришёл после DEL.
Устаревание ограничено TTL, DEL не даёт мгновенной согласованности. Для XYZ
пропавший ожидаемый файл после cache hit потребует `force=True` и проверки БД,
затем 410 либо 503 согласно контракту; сам XYZ handler относится к этапу 7.

## Доказательства

`backend/tests/test_tile_lifecycle_database.py` содержит 14 интеграционных gates:
реальные locks/fencing и heartbeat, независимость форматов, ready pin,
неизменяемость правил/manifest, dataset cleanup с active+prev обоих форматов,
recovery до/после публикации, запрет orphan ready/чужих pointers, grants/RLS,
конкурентный pin против cleanup с наблюдением advisory wait, Redis hit/miss,
invalidate, stale fill/TTL/force, Redis outage, async/corrupt cache,
tombstone после удаления snapshot и SQL-модель rollback с datasetVersion.

Полный backend suite: 99 passed, 0 skipped, 27.76 s. После дополнительной проверки
cross-format UPDATE повторно пройдены все 14 lifecycle gates. Ruff passed.
Docker tiles-worker собран; импорты heartbeat/cache/Redis работают,
Cairo 1.16.0 / WebP 1.6.0 / redis-py 6.4.0. Осталась прежняя deprecation
Starlette/httpx TestClient.

Этап 3 принят. Следующий этап — ручной vector CLI, полный staging bake,
manifest, validate и отчёт по zoom; production publish пока отсутствует.
