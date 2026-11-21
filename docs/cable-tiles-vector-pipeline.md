# Полная vector-сборка: этап 4

2026-10-08. `map-tiles vector --dataset <UUID>` собирает полный sparse-набор
native MVT на явном ready snapshot. Команда не меняет active и не переносит
staging в final. Publish/rollback/cleanup реализованы отдельно: [этап 6](cable-tiles-publication.md).

## Запуск

Установка worker-зависимостей и CLI:

```sh
uv sync --project backend --extra tiles
backend/.venv/bin/map-tiles --env .env vector --dataset <UUID>
```

`--env` задаётся перед subcommand. Обязательный worker DSN:
`MAP_TILES_VECTOR_DATABASE_URL` с ролью `map_tiles_vector`, отдельной от API,
owner и исходной Userside. Deployment init создаёт роль; LOGIN включается при
наличии `MAP_TILES_VECTOR_DB_PASSWORD`. В disposable проверке пароль/роль
установлены через те же helpers. Source credentials worker не использует.

Настройки:

| Переменная | Default | Назначение |
| --- | --- | --- |
| `TILES_ROOT` | `./data/tiles` | Корень выпусков |
| `US_SOURCE_SYSTEM` | `userside` | Область snapshot |
| `US_MAP_ID` | `3` | Карта |
| `TILES_MINZOOM` | `1` | Начало native coverage |
| `TILES_VECTOR_MAXZOOM` | `14` | Конец native coverage, минимум 14 |
| `TILES_DISPLAY_MAXZOOM` | `18` | Клиентский overzoom |
| `TILES_CABLE_TYPE_IDS` | `1` | Положительные IDs через запятую |
| `TILES_CONCURRENCY` | `2` | 1–8 независимых render connections |
| `MAP_REDIS_URL` | отсутствует | Status invalidation; без него читается БД |

Native minzoom ≤14≤maxzoom≤displayMaxzoom≤24. UUID без ready snapshot или
с чужими source/map отклоняется до создания каталога. БД с Userside-таблицей
отклоняется. CLI не выводит DSN/пароли при ошибках.

## Сборка и validate

`map_tiles.pipeline.bake_vector` держит session lease всего запуска, фиксирует
UUID/rules hash/geometry profile в BuildSpec и pin snapshot. Hash правил включает
реальный код общего cable predicate/style и набор cableTypeIds. Общий canon
содержит tolerance table как минимум z0–15; при defaults совпадает с raster
prototype hash `d7ff5b2412a64bd9efd65b4bb4efee937fc7c241a8ba3241543a7f10d0a1fbc3`.

Planner обходит каждый native zoom. MVT записываются сырыми PBF в
`<mapId>/vector/.staging/<tileVersion>/<z>/<x>/<y>.pbf`. Отбор и encode используют
тот же simplify/clip/buffer, что принятый прототип. Dash, scalar style,
`layerIds=,id,`, objectId и short-line retention сохранены.

Две worker threads используют собственные autocommit connections; после
каждого пакета ≤concurrency×64 задач pool и connections закрываются. Vector
рендер выполняется в PostGIS; Python делает decode и файловую запись. Cairo
и process-memory ограничения raster будут использовать отдельный runner.
DB connect_timeout=5 s, statement_timeout=30 s; отдельный heartbeat обновляет
сборку и проверяет основной advisory lock. Pipeline проверяет lease между
пакетами, во время disk validate и до смены состояния.

`manifest.json` содержит происхождение, правила/hash, zoom, WGS84 bounds,
encoding, canon/hash и native coverage по каждому zoom. Каждый prebaked tile:
XYZ, канонический path, bytes, SHA256 и sorted objectIds входа. Нет записи
внутри native — геометрически пустой тайл; отсутствие ожидаемого файла — ошибка.
Overzoom хранится диапазоном maxzoom+1…displayMaxzoom, файлов для него нет.

`map_tiles.manifest.validate_staging` проверяет полный диапазон zoom,
дубли/пути/status, наличие и checksum каждого файла, отсутствие дополнительных
файлов/симлинков, совпадение disk manifest. Независимый mapbox-vector-tile
2.2 decoder проверяет слой/extent/линейную геометрию/атрибуты и точное совпадение
objectId с входом каждого тайла. Decode и parity проверяются также до записи.
Только после disk gate версия становится validated, build — succeeded.

`report.json` и CLI JSON-lines содержат zoom, tile count, bytes, seconds;
final report отдельно содержит validationSeconds. Staging сохраняется для
публикации. Manifest после validated неизменяем в БД.

Отмена Ctrl+C: CLI code 130, build failed/BUILD_CANCELLED. В начале/конце bake
выполняется cleanup. Если он успешен, failed staging удаляется и остаётся deleted
tombstone; при ошибке GC staging остаётся с cleanup=error, новая сборка блокируется
до успешного повторного cleanup. Retry создаёт новый UUID. При потере lease
pipeline не меняет состояние без блокировки, применяется locked recovery.
Команды и порядок publish/rollback/GC: [этап 6](cable-tiles-publication.md).

## Доказательства

Пять интеграционных тестов `backend/tests/test_vector_pipeline.py`: полный
z1–14 bake fixtures, short cable/group/layers/границы и native union; missing,
corrupt и unexpected file; чужой/неизвестный dataset; отмена/pending/retry;
worker failure без оставшихся threads; CLI требует явный dataset.
World-spanning planar antimeridian line исключена из массового fixture bake,
чтобы не создавать 32768 high-zoom tiles; край света проверен отдельными
PostGIS/browser gates этапа 2. Ограничения production coverage не введены.

Полная копия immutable snapshot `281f5711-c66c-4ecb-9046-f8146c25bed3`
(7113 features) импортирована в disposable PostGIS. Настоящий CLI работал с
LOGIN ролью `map_tiles_vector`, не owner. Все 1912 MVT прошли disk decode/checksum
и точное сравнение входных ID. Native z14 union = 3266 IDs и совпал с SQL none;
active остался пустым. Сырые PBF занимают 1 422 178 bytes, manifest — около
0.99 MB. Замеры и provenance: `reports/tiles/vector-cli-full.jsonl`,
`vector-cli-audit.json`, `vector-cli-manifest.json`.

Docker worker собирается и содержит `map-tiles` и production decoder. Итоговый
regression результат записан в `cable-tiles-validation.md`. Рабочая БД не менялась.
