# Состояние реализации и проверки тайлов

2026-10-09. Этапы 1–10 приняты. Vector/raster опубликованы в рабочем локальном
Compose, Nuxt переключает none/vector/raster, GIS API проверен по HTTP.
Ниже сохранена история проверок и итоговая матрица приёмки.

## Выполнено

- Этап 1: сопоставлены текущие SQL/API, snapshot сборка и MapLibre стиль.
  `map_render/cables.py` подключён к объектному API и порогам sync;
  ID/стили/семантика выбора сохранены. Provider использует общие source kinds.
- Новые snapshots включают isBackbone для самостоятельных кабелей и
  представителя группы. Существующие snapshots неизменяемы и не переписаны.
- [Контракты](cable-tiles-contracts.md): catalog active+prev/datasetVersion,
  TileJSON, style, details, manifest/coverage, media types, HMAC A/B, pin, ошибки.
- Fixtures: short/long/solid/dashed/multipart/hidden/types/layers/tile edge,
  antimeridian/latitude. Проверены PostGIS и browser gates этапа 2.
- Этап 2 принят: общий вход geom3857/tol, PostGIS MVT + независимый decode,
  Cairo→straight RGBA→WebP 1x/@2x; tile style helper; Docker worker target.
  Браузерный прототип и полные zoom/coverage замеры выполнены;
  параметры и доказательства: [prototype](cable-tiles-prototype.md).
- Этап 3 принят: lifecycle migration, constraints/FK/RLS, locks/heartbeat/recovery,
  snapshot protection, tombstones и sync/async Redis status cache.
  [Модель и доказательства](cable-tiles-lifecycle.md).
- Этап 4 принят: vector CLI, полный sparse native bake, manifest/checksums,
  independent decode и parity, validated staging, отмена/retry и zoom report.
  [Запуск и доказательства](cable-tiles-vector-pipeline.md).
- Этап 5 принят: raster CLI, полный WebP prebake 1x/@2x, spawn pool,
  checksum/decode/coverage validate; native input union совпал с vector и none.
  [Pipeline и доказательства](cable-tiles-raster-pipeline.md).
- Этап 6 принят: publish active+prev, rollback с datasetVersion, FS GC/tombstones,
  recovery rename/commit/delete, независимость форматов, Redis invalidate.
  [Операции и доказательства](cable-tiles-publication.md).
- Этап 7 принят: scoped auth/token CLI, catalog/TileJSON/style/details,
  pinned preload/batch, XYZ Redis hot path, gzip/WebP и HMAC templates.
  [API и доказательства](cable-tiles-api.md).
- Этап 8 принят: Nuxt none/vector, catalog ETag/proxy, pin точек, полный details
  overlay, тематические фильтры, cold start, measure без snap и lag banner.
  [Frontend и доказательства](cable-tiles-frontend.md).
- Этап 9 принят: raster/@2x, переключение трёх режимов, focus/poll/expiry/410,
  один refresh с backoff, новая подпись без reset, новая версия с cancel/reset.
  [Frontend и browser gates](cable-tiles-frontend.md).
- Этап 10 принят: отдельные workers, RO API volume, Redis, readiness, метрики,
  алерты с измеренным grace, рабочая публикация и production browser gates.
  [Эксплуатация и GIS](cable-tiles-operations.md).
- Docker упаковка включает новые пакеты; .dockerignore разрешает их копирование.

## Доказательства

На отдельном `postgis/postgis:16-3.5` (PostgreSQL 16, PostGIS 3.5.2,
GEOS 3.9.0), не на рабочей карте и не на Userside:

```text
DYLD_FALLBACK_LIBRARY_PATH=<local Cairo lib>
TEST_MAP_DB_URL=<disposable PostGIS>
backend/.venv/bin/python -m pytest backend/tests -q
80 passed, 0 skipped, 1 warning, 22.29s
```

Warning — существующая deprecation Starlette/httpx TestClient.
SQL parity тесты сравнивают прежний SQL, общие предикаты и фактические
HTTP preload/batch по ID при z13/z14 и layer1/layer11. MVT тест сверяет
декодированные objectId с входом, включая short cable и multipart group.
Raster тесты проверяют transparent tiles, antialias straight RGB без тёмного
ореола, непрерывность former-dashed, порядок backbone, размеры 256/512 и
логическую ширину 3px при обеих плотностях. Это не pixel-diff gate.

```text
frontend: npm test -- --reporter=dot → 24 passed (2 files)
frontend: npm run typecheck → exit 0
ruff check changed backend packages/tests → All checks passed
docker build --target tiles-worker → exit 0
docker run --rm userside-map-tiles-prototype:local → Cairo 1.16.0 WebP 1.6.0
```

Запуск Docker Desktop восстановил существующие сервисы compose; их данные не
менялись. Для интеграционного прогона создан отдельный временный контейнер
`map-tiles-test-postgis` с отдельной БД, затем удалён после проверок.

## История промежуточных проверок

Этапы 1–9 приняты. Добавлены reference frames native/overzoom при DPR1/DPR2,
проверены theme filters, кабели под узлами, short cable, запросы native XYZ при
overzoom, gzip, край света и предельные широты. Замеры выполнены на отдельной
копии active dataset; исходная БД читалась, active pointers не менялись.
Зафиксированы vector_maxzoom=14, raster_prebake_max=15, displayMaxzoom=18,
buffer=8 XYZ px, concurrency=2, отдельные поколения pool для ограничения RSS.
При raster tileSize=256 учтён XYZ zoom=q+1, отбор min_zoom<=z-1.

Сверены 1 912 MVT с diagnostic inputs по каждому тайлу; native union
vector14/raster15 совпал с SQL none (3 266 ID). Прототип default-cycle занял
81.09s, peak raster worker RSS=234.59 MiB. Это render/prebake без lifecycle,
validate/publish/GC; эксплуатационный grace period ещё не выбран.
Артефакты в reports/tiles и docs/assets/cable-tiles-prototype.
Повторный полный regression suite после всех исправлений: 85 passed,
0 skipped, 23.35s (одна прежняя Starlette/httpx deprecation). Frontend: 24
passed, typecheck exit 0. Cairo decoder дополнительно проверен с padding stride
и обеими endianness. Последний Docker worker image также собран успешно.

Дополнительно исправлены: потеря короткого кабеля GeoJSON tolerance в none,
низкий zoom с buffer за ±180° и потеря tiny MVT при квантовании. Sparse planner
использует тот же simplify, что renderer. После этого принят этап 3: 99 backend tests passed, 0 skipped, 27.76s;
14 lifecycle gates на PostGIS и Redis. Docker worker повторно собран,
Ruff passed. Рабочие данные и pointers не менялись.

Этап 4: полный CLI на копии 7113 features под LOGIN ролью map_tiles_vector:
1912 MVT, 3266 native IDs = SQL none, validated без active publish. Bake +
validate 19.83s (disk validate 2.25s), raw PBF 1 422 178 bytes. Manifest и
отчёты сохранены в reports/tiles/vector-cli-*. Все 104 backend tests прошли
без пропусков за 45.83s; Ruff и Docker worker build/CLI/decode smoke прошли.
Этап 5: Docker raster CLI на том же dataset, отдельная LOGIN роль;
2 CPU / 1 GiB, 8764 WebP проверены, native union 3266 IDs = vector14 = SQL none.
Render+validate 83.48s, disk validate 9.42s, peak Linux worker RSS 84.39 MiB.
108 backend tests passed, 0 skipped, 73.41s; Ruff и Docker full bake прошли.
WebP 1x+2x = 17 427 200 bytes; retention/staging estimate и allocated disk
замер в reports/tiles/raster-cli-audit.json. Visual frame из нового pipeline
сохранён в docs/assets/cable-tiles-prototype/raster-pipeline-native15-2x.png.
Этап 6: 120 backend regression tests passed, 0 skipped, 124.45s. После
расширения retention/grants/cache gates на оба формата: 14 publication tests
passed, 0 skipped, 68.99s. Ruff passed; Docker worker построен и выполнил
реальные vector CLI bake/publish/retry/targeted rollback/cleanup как uid10001,
LOGIN map_tiles_vector на отдельном fixture map4. Reports/tiles/publication-cli.json.
Проверены реальный deferred commit failure, before/after rename/commit/GC,
≤2 ready/final на каждый формат, datasetVersion и неизменность файлов при
rollback, pending GC/format independence, symlink safety и Redis invalidate.
Этап 7 (2026-10-09): 131 backend tests passed, 0 skipped, 167.79s. После
последних HTTP headers/registry/isolation изменений: 12 API/HTTP tests passed
(26.65s), затем окончательные 9 tile API gates passed (26.62s). Ruff passed.
Проверены реальная map_api роль и token CLI, API Docker build без Pillow/Cairo,
ASGI wire gzip/empty MVT, WebP/@2x, auth/ETag/CORS, HMAC expiry/renewed templates,
no-DB cache hit/Redis down, stale ready+missing → DB → 410/503, HTTP rollback
на другой datasetVersion/lag, pin только вне none и authenticated pinned batch.
Token plaintext в reports не сохранён; рабочие данные не изменены.
Этап 8 (2026-10-09): 32 frontend tests passed, typecheck exit0, production build
exit0. Nuxt proxy HTTP: 304, карта/путь allowlist, server token отсутствует в SSR.
Реальный Nuxt + FastAPI + disposable PostGIS + headless Chrome: native vector14
с overzoom на карте15, promoteId, кабели под точками, точные theme1/11 filters,
полный cable_group:5 overlay, измерение без snap, переключение none без pin.
Проверен новый пустой active dataset: node100 приходит из retained vector dataset,
lag banner виден, live catalog не отключает pinned providers. Browser uncaught
exceptions:0. reports/tiles/frontend-vector/audit.json и vector-selected.png.
Повторный browser gate после последних исправлений прошёл. Helper shutdown
проверен с pinned и none leases: собственные DB rows и private token удалены.
Disposable контейнер удалён. Рабочая карта и её
pointers не менялись. Vite сообщает о прежнем крупном MapLibre chunk.

Этап 9: 41 frontend tests passed; typecheck/build passed. Реальный browser gate
при DPR1/2: native raster15/tileSize256/@2x под точками; raster node101 и vector
node100 из разных datasets; cable hit-test отсутствует. При HMAC TTL10/poll0
подпись обновляется без point preload/selection reset; freeze11s/focus восстановлен.
Два реальных publish retire старую vector-версию: HTTP410 (первым может прийти
pinned preload, отменяющий старые XYZ) вызывает ровно один catalog request,
смену источника/pin и сброс выбора. Native callback burst100 проверен unit gate.
41 tests также проверяют backoff/auth stop/invalid pause/poll/destroy. Reports:
reports/tiles/frontend-raster-refresh/audit.json и raster-dpr{1,2}.png. Dev HMR
отключён в sleep harness; production его не содержит. Источник кабелей при смене
подписи пересоздаётся: MapLibre не повторяет cached XYZ со старым подписанным URL.

На момент приёмки этапа 9 рабочие pointers ещё не публиковались новым pipeline.
Эта эксплуатационная проверка выполнена ниже, в этапе 10.

## Итоговая приёмка этапа 10

В исходном Compose применены миграции 008–010; active snapshot
`281f5711-c66c-4ecb-9046-f8146c25bed3` остался прежним, его 7 113 features
не переписаны. Новый sync и запросы к Userside не выполнялись. Два отдельных
CLI собрали и опубликовали vector/raster для этого явного UUID.

Полный последовательный цикл bake/validation/publish обоих форматов занял
123.001s: vector 33.889s, raster 89.111s. Worker: 2 CPU, 1 GiB, concurrency2;
peak raster worker RSS 84.56 MiB. Созданы 1 912 MVT и 8 764 WebP (4 382 XYZ ×
две плотности). Проверочный pipeline вызывает GC при publish; в первом рабочем
выпуске вытеснять prev ещё не требовалось. Crash/GC/retention сценарии прошли
отдельные реальные lifecycle/publication gates. Grace выбран 210s:
1.5×123.001, округление вверх до 30s. Переменная задаётся в Compose, API и
`.env.example`; настройки нужны для конкретной скорости нового окружения.

`/metrics` читает durable состояние БД, содержит lag/retained/cleanup/build и
cache counters. Promtool проверил конфигурацию и четыре правила, а unit rules
подтвердили grace и immediate failed-build. Рабочий Prometheus scrape up;
все правила healthy/inactive при совпадающем dataset. Notification receivers
не настроены; мониторинг доступен локально на :9090.

API роль map_api не может UPDATE snapshot/tile_versions, не получает owner или
Userside credentials. `/tiles` смонтирован RO; workers получают RW и только
свой LOGIN role. Redis порт закрыт. Реальное отключение Redis сохранило XYZ200
и readiness200; fallback запрос занял 0.112s, после запуска cache hits возобновились.
API access log выключен. Catalog/TileJSON/style/details используют scoped Bearer;
401 без токена, 403 для чужой карты, ETag304 и no-listing подтверждены.

Production Nuxt + FastAPI + рабочий snapshot прошли CDP при DPR1 и DPR2:
три режима, native14/vector и native15/raster при display zoom18, raster @2x,
pin точек без cable_line в preload, none без pin. Через обычное UI pan выбран
кабель с муфтой: изображения показывают кабели под точкой/подписью. Нет dev
diagnostics, browser Bearer или uncaught exceptions. JPEG используют quality90:
большие PNG DPR2 вызывали обрыв CDP WebSocket в локальном Chrome; продуктовый
рендеринг и HTTP работали. Raster overzoom увеличивает экранную толщину и
сглаживание готового изображения; цвета/иерархия ширин узнаваемы, pixel equality
с vector не заявляется.

Повторная независимая сверка рабочего native14 MVT decode (1 096 тайлов),
native15 raster inputs (2 484 тайла) и SQL none дала **3 266 одинаковых ID**.
Рабочий HTTP подтвердил gzip MVT, WebP256/512, пустые корректные тайлы,
ETag304, native overflow422 и полный details того же dataset.

Реальный Nginx `1.28-alpine` с proxy_cache_valid200=1h проверен перед disposable
API в режиме B: s-maxage0/must-revalidate заставляют повторный запрос попасть
в origin, HIT не обслуживается без проверки. После expiry тот же URL с прежним
If-None-Match дал **403**, а не cached200/304. Stale serving выключен.
Тест использует часы fixture и dummy secret; секреты рабочего deployment не
менялись. Локальный рабочий XYZ режим A; внешний TLS/VPN/SSO и CDN привязываются
по [гайду](cable-tiles-operations.md), публичный hostname не развёртывался.

Итоговые проверки:

```text
backend full suite: 134 passed, 0 skipped, 174.31s
Nginx HMAC/cache integration: 1 passed, 3.37s
frontend: 41 passed (5 files), typecheck passed
Docker API/worker/frontend builds: passed (Nuxt production build included)
ruff changed implementation/tests/deploy scripts: passed
promtool config/rules/unit rules: passed
npm audit --omit=dev: 0 vulnerabilities
```

Есть прежняя Starlette/httpx TestClient deprecation и большой MapLibre build
chunk. `npm ci` также сообщает об advisories в dev/build dependency tree;
runtime audit выше их не содержит. Обновление всего toolchain не входило в
тайловую архитектуру. Worker_fill, block_complete и административный UI
остаются отдельными будущими фазами по плану.

## Матрица критериев завершения

| Критерий | Подтверждение |
| --- | --- |
| Раздельные CLI с одним dataset, sync ≠ tiles | deployment-cycle.json; operations guide |
| Полный native bake, short cable, edge/latitude, maxzoom14/15 и overzoom18 | render/pipeline backend tests; prototype reports; production browser audit |
| SQL none/vector/raster ID parity | deployment-parity.json: 3 266 ID |
| Shared geom3857/tolerance, frozen rules, immutable releases | manifest/geometry tests; lifecycle DB constraints |
| ≤2 ready/format, active+prev, rollback dataset provenance | lifecycle/publication/API tests; working ready count1/format |
| Locks, heartbeat, recovery, cancel, rename/GC crashes, pending cleanup | lifecycle/publication gates; full regression |
| Redis TTL, invalidate race, fallback/recovery | lifecycle/API tests; deployment-redis.json |
| Gzip MVT, WebP1x/2x, valid empty200, ETag/error no-store | API tests; deployment-api.json |
| Catalog/TileJSON/style/details Bearer; pinned points/details | API auth/rollback tests; deployment API/browser |
| Delimited layerIds1/11, cables under points, full details overlay, no snap | frontend-vector audit; unit tests; production screenshots |
| Three modes, raster capability limits, cold start | 41 frontend tests; raster-refresh and deployment browser audits |
| banner/poll0, HMAC refresh before expiry/focus, one flight/backoff, 410 | frontend-raster-refresh audit and unit tests |
| HMAC prefix + before304 + CDN cache-hit protection B | tile API tests; deployment-proxy.json; proxy guide |
| .env/Compose, default flag restart, RO API, role-separated workers, no listing | deployment-ops.json; deployment-api.json; guide |
| Measured grace, immediate failed alert, readiness/log secrecy | deployment-cycle/ops/redis reports; Promtool rules; API operations tests |
| Widths recognizable, without pixel equality | prototype frames; deployment browser vector/raster DPR1/2; documented overzoom limit |

Артефакты этапа 10: `reports/tiles/deployment-{cycle,api,parity,redis,proxy,ops}.json`,
`reports/tiles/deployment-browser/audit.json`, `*-dpr{1,2}.jpg`.
Инструменты воспроизведения: `scripts/validate-tile-compose-cycle.py`,
`scripts/validate-tile-deployment-api.py`, `scripts/validate-tile-deployment-browser.mjs`,
`scripts/test_tile_proxy_gate.py`. Отчёты не содержат токенов, DSN или HMAC URL.
