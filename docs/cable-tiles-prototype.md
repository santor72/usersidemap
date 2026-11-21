# Прототип кабельных тайлов

2026-10-08. Этапы 1–2 приняты. Lifecycle/publish и рабочий tile API/UI ещё не
реализованы; ниже результаты прототипа без публикации active.

## Параметры v1

| Параметр | Значение |
| --- | --- |
| vector_maxzoom | 14 (XYZ) |
| raster_prebake_max | 15 (XYZ), обе плотности полностью |
| displayMaxzoom | 18 (zoom карты), выше native только overzoom |
| MVT extent / buffer | 4096 / 128 units (8 XYZ px) |
| vector source tileSize | 512 (обязательное значение MapLibre) |
| raster tileSize | 256 логических px; 1x 256, @2x 512 |
| concurrency | 2 процесса; новое поколение pool каждые ≤128 тайлов |
| worker memory limit для следующих этапов | 1 GiB; CPU limit 2 |
| стратегия | полный sparse prebake, без worker_fill и дописывания выпуска |

Defaults: `backend/map_tiles/defaults.py`. Buffer выбран по максимальной
ширине 5px контрольного и рабочего dataset. Полная сборка должна проверять
совместимость ширины с buffer; новые параметры — новый выпуск. Для коротких
кабелей raster native должен быть ≥15 при tileSize=256.

MapLibre 6.12.0 vector source имеет tileSize=512, raster 256 использует
round(mapZoom+1). На целых zoom vector запрашивает XYZ q, raster XYZ q+1.
Поэтому visibility selection raster=z-1, vector=z, геометрический tol(z)
общий по XYZ. На дробных zoom raster меняет уровень при половине zoom;
состав изображения дискретный, покомпонентный min_zoom/filter отсутствует.
Source.maxzoom остаётся native XYZ, не displayMaxzoom. Сверено origin audit
и [covering_tiles.ts](https://github.com/maplibre/maplibre-gl-js/blob/v6.12.0/src/geo/projection/covering_tiles.ts).

## Renderer и канон

`vector.py`: выбор snapshot → проекция → общий simplify → MVT.
`raster.py`: тот же canon → Cairo solid, non-backbone снизу → WebP.
`planning.py`: пересечение частей с буферной сеткой после того же simplify,
чтобы не терять coverage при изменении формы. `cableTileLayers` повторно
использует paint/dash объектного режима.

tol(z): 0.5 XYZ px в метрах для z<14, 0 для z≥14.
ST_Simplify(...,preserveCollapsed=true) после проекции в 3857.
Граница Mercator: ST_ClipByBox2D в 4326 по ±85.0511287798066, затем ST_Transform.
Buffer-область перед обратным transform ограничивается размером мира: иначе
PROJ заворачивал longitude за 180° и отбор z1 становился пустым.
ST_Intersection в проверенном GEOS 3.9.0 терял горизонтальную линию внутри
envelope; применяется ST_ClipByBox2D и regression gate.

Короткая линия, которую ST_AsMVTGeom удалил при квантовании, сохраняется как
сегмент в одну extent unit в точке clipped line. Это ограниченная погрешность
MVT-сетки, а не потеря identity. Пустой слой cable_line содержит name, extent
и version, декодируется независимой библиотекой. PBF на диске сырой, gzip
добавляется при HTTP отдаче. GeoJSON source в none получил tolerance=0:
стандартное упрощение geojson-vt теряло контрольный короткий кабель при z14.

Совместный профиль z0..15: SHA256 canonical sorted JSON
`d7ff5b2412a64bd9efd65b4bb4efee937fc7c241a8ba3241543a7f10d0a1fbc3`.
Профиль включает алгоритм, проекцию, tol, buffer, extent, tileSize, quantization
fallback; canon(15) воспроизводит hash. Оба pipeline фиксируют один профиль
в начале сборки; диапазон файлов различается по формату.

## Cairo/WebP

Worker extra: cairocffi 1.7.1, Pillow 12.1.0 (uv.lock).
Docker target tiles-worker: Cairo 1.16.0, WebP 1.6.0, Python 3.12.
MacOS замер: Cairo 1.18.4, тот же WebP. Codec: lossless=true, quality=80,
method=4, exact=true. Native-endian premultiplied ARGB32 → Pillow RGBa raw
decoder (stride/endianness) → straight RGBA → отдельный WebP. Преобразование
в C убрало дорогой Python цикл по каждому пикселю.

Проверены обе плотности, прозрачный фон, straight RGB antialias без тёмного
ореола, логическая ширина 3px, solid у former-dashed, порядок backbone.
Docker renderer также прошёл проверки alpha/solid/размеров. Прототип пишет
в отдельные временные каталоги, публикацию не выполняет.

## Замеры

Копия active dataset `281f5711-c66c-4ecb-9046-f8146c25bed3`: 7 113 features,
3 391 cable rows, 3 266 разрешённых видимых cable objectId. Source/export
только читались; импорт и измерения в disposable PostGIS 16 / PostGIS 3.5.2.
Userside и рабочие active pointers не менялись.

| Уровень | Непустых XYZ | Payload |
| --- | ---: | ---: |
| vector z14 | 1 096 | 538 251 B |
| vector z15 (сравнительный эксперимент) | 2 484 | 888 713 B |
| raster z15, обе плотности | 2 484 | 8 414 862 B |
| raster z16 (эксперимент), обе плотности | 5 484 | 15 664 232 B |

Выбранные native обеспечивают short cable при zoom карты 14 и overzoom до
18. Дополнительный уровень более чем удваивает tile count, поэтому v1
оставляет vector14/raster15. Higher-native report содержит ранний код до
исправлений низких zoom; его время не используется как оценка полного цикла.
Исправленный полный цикл с persistent worker connections: 40.05s при 4
процессах, 1 912 MVT и 4 382 raster XYZ (8 764 WebP). Для эксплуатации выбраны
2 процесса: меньше суммарная память. Итоговый default-cycle с двумя workers
и ограниченными поколениями: 81.09s, peak RSS одного raster worker 234.59 MiB,
`reports/tiles/prototype-default-cycle.json`. Повторно сверены все MVT и
native union ID обоих форматов; bytes/hash совпали с исправленным 4-worker run.

Без смены поколений RSS raster worker достигал 271 MiB. С ограниченными
поколениями — ≤233 MiB на контрольном замере 4 workers. Планируется limit
1 GiB при concurrency=2. Python 3.12.2 executor с max_tasks_per_child остановил
создание workers после 256 задач; явные поколения pool прошли полный уровень.

Payload полного coverage: vector 1 422 178 B, raster обе плотности
17 427 848 B. APFS allocations без diagnostic sidecars: 8 065 024 и
41 082 880 B. Две retained каждого формата + staging обоих одновременно:
≈147.4 MB allocation / 56.6 MB payload, плюс directory/manifest и отчёты.
Резервирование диска учитывает block size и рост dataset, не только WebP size.

Артефакты в reports/tiles: prototype-cycle.json, higher-native-experiment,
concurrency2, bounded-workers и default-cycle. Grace period алерта выбирается
повторно по full pipeline с validate/publish/GC на этапе 10.

## Приёмка

Chrome 154 / MapLibre 6.12.0: native14, z13, overzoom18; DPR1 и DPR2.
Rendered ID none/vector совпадают; short cable жив; ,1, не совпадает с ,11,;
точки сверху. Raster solid, без фильтра тематики. Origin audit подтверждает
vector XYZ≤14 и raster XYZ≤15 при overzoom; @2x только при DPR2; MVT gzip.
Ширины узнаваемы, не pixel-identical: raster overzoom увеличивает и размывает
линию. Seams проверены в кадрах, renderer сохраняет обе стороны tile edge.

![Native](assets/cable-tiles-prototype/native-dpr1.png)
![Short cable overzoom](assets/cable-tiles-prototype/overzoom-dpr1.png)

Все 1 912 MVT: decoded IDs = diagnostic input IDs по каждому тайлу.
На native vector14/raster15 union inputs = SQL none на полной карте, 3 266 ID.
PostGIS gates: tile edge с обеих сторон, antimeridian, предел широт,
coordinates в buffer, tiny line на низком zoom, multipart IDs, raster offset.
Pixel diff не gate. Проверки: 85 backend tests без skips; 24 frontend tests,
typecheck/Ruff и Docker сборка проходят. Дополнительно проверены stride и
обе endianness в Cairo alpha decoder.

## Воспроизведение

```sh
uv sync --project backend --extra tiles
# TILE_PROTOTYPE_DB_URL — только disposable PostGIS.
uv run --project backend --extra tiles python scripts/cable_tiles_prototype.py import backend/tests/fixtures/cable_tiles.json
uv run --project backend --extra tiles python scripts/cable_tiles_prototype.py bake \
  --dataset <returned-UUID> --output /tmp/tiles-fixtures --bbox 36.98 54.99 37.031 55.011
frontend/node_modules/.bin/esbuild frontend/modules/cartography/runtime/adapters/maplibre/cable-style.ts \
  --bundle --format=esm --outfile=/tmp/cable-tiles-style.js
uv run --project backend --extra tiles python scripts/cable_tiles_prototype.py serve \
  --output /tmp/tiles-fixtures --style /tmp/cable-tiles-style.js
# Dedicated headless Chrome CDP port 9225, never a shared browser profile.
node scripts/validate-cable-tiles-prototype.mjs
docker build --target tiles-worker -t userside-map-tiles-prototype:local .
```

На macOS Cairo должен быть доступен динамическому загрузчику; в Docker
дополнительных путей не требуется. HTTP server — локальный тестовый инструмент,
не production XYZ API; auth/lifecycle реализуются на этапе 7.
