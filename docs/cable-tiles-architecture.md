# Архитектура тайлов кабелей

Дата: 2026-10-07, уточнено 2026-10-08. Статус: проект архитектуры для
последовательной реализации. Документ расширяет [текущую архитектуру](architecture.md),
[контракт API](map-api.md) и [описание сборки кабелей](lines_pashe1.md).
Текущий прогресс pipeline и тайловых режимов:
[cable-tiles-validation.md](cable-tiles-validation.md).

## Решения и границы

Согласовано с пользователем:

- Настройка `cable_tiles=none|vector|raster` — **на деплой** (env). Выбирает способ
  отображения кабелей в UI. Не управляет bake. Смена значения требует
  **restart** процессов приложения (без hot-reload).
- `none` — существующая загрузка кабелей объектами (`active_datasets`, без pin).
- `vector` / `raster` — запеченные тайлы соответствующего формата.
- Pipeline vector и raster — **раздельные ручные CLI**; `map_sync` их **не**
  запускает. Приложение не оркестрирует bake. **Ops-правило:** после одного
  sync админ обязан запечь **оба** формата на одном dataset (зона
  ответственности админа, не кода). Оба запуска получают один явный обязательный
  `--dataset <UUID>`; CLI проверяет принадлежность dataset карте и статус ready.
  Новая синхронизация между запусками не меняет выбранный dataset.
  Технически CLI независимы; расхождение dataset между форматами — ошибка процесса.
  Алерт «format behind» учитывает настраиваемый период ожидания последовательных сборок.
- Выпуски технически **независимы по формату**: отдельные сборки, манифесты,
  active/prev, publish/cleanup. Сбой raster не блокирует vector и наоборот.
- Для `raster`: без фильтров и кликов по кабелям; все линии **solid** (цвет,
  ширина, opacity, порядок из канона; dash игнорируется). `none`/`vector`
  сохраняют solid/dashed.
- Raster-рендер: **Cairo** (pycairo) в worker/CLI; без MapLibre Native и
  headless-браузера. FastAPI не рендерит тайлы.
- Покрытие zoom (coverage):
  - **vector** — полный plan непустых тайлов до `vector_maxzoom`, минимум z14;
  - **raster** — полный prebake до `raster_prebake_max`, минимум z14;
  - выше готового уровня — только клиентский **overzoom** до `displayMaxzoom`;
  - стартовый native-предел обоих форматов — z14; необходимость z15 и выше
    определяется измерениями этапа 2. Нельзя снижать предел ниже максимального
    min_zoom разрешенных кабелей, иначе overzoom не вернет отсутствующие объекты;
  - в манифесте: `coverage: prebaked | overzoom | missing`.
- Retention: строго **active + prev** (две успешные версии) **на каждый формат**
  отдельно. Аренды/TTL не продлевают жизнь. Удаление — в pipeline формата.
- Авторизация XYZ (default **A**, опция **B**):
  - **A:** UUID версии в path + проверка ready/retired (БД, с кэшем Redis
    `version→status`); без Bearer на файл;
  - **B (опция):** то же + HMAC `exp`/`sig` на **template / path-prefix**
    (не на каждый z/x/y) для публичного интернета/CDN (`TILES_XYZ_SIGNING=hmac`);
  - метаданные — Bearer / Nuxt session; Nuxt proxy только `/api/...`;
    XYZ напрямую на `/tiles/...`.
- Pin dataset точек к tileVersion — только в `vector|raster`. `none` — без pin.
- Холодный старт / нет ready нужного формата: UI **остаётся на `none`**; админ
  печёт оба формата, затем выставляет `cable_tiles` и **restart**. Автоfallback
  runtime → `none` нет.
- Измерение: без snap к кабелям. В MapLibre кабели (vector/raster source)
  **под** слоями узлов и муфт.
- Lag: индикаторы в каталоге; default `tiles_lag=banner` (баннер на карте).
  `tiles_lag=block_complete` отложен до отдельной интеграции с админкой;
  первая версия поддерживает `banner`, не блокирует карту или завершение sync.
  Алерт при отставании bake; sync tiles не чинит.

Принятые начальные технические параметры:

- Vector: MVT/PBF с **`Content-Encoding: gzip`**; raster: прозрачный **WebP**
  (+ **`@2x`**).
- Retina raster: `@2x` при `devicePixelRatio ≥ 1.5`; логический `tileSize` = 256.
  Настройку raster tileSize не переносить на vector. На этапе 2 проверить
  соответствие zoom карты, XYZ-уровня и порогов min_zoom для обоих источников.
- Zoom-ориентиры (этап 2): `vector_maxzoom` и `raster_prebake_max` ≥ 14;
  `displayMaxzoom` задает рекомендуемый предел увеличения карты отдельно.
- XYZ Web Mercator, `z/x/y`; `mapId` / `format` / `version` в **path**.
- Catalog: ETag; focus/visibility; 410 → refresh. Default
  `TILES_CATALOG_POLL_SECONDS=0` (poll выкл).
- MVT extent 4096; buffer/`tol(z)` — прототип. Raster: buffer по ширине линии.
- **`layerIds` wire-format (фиксирован):** строка с ведущей и завершающей
  запятой, id через запятую без пробелов, например `,12,34,56,`.
  Фильтр MapLibre: `index-of` подстроки `,{id},` ≥ 0. Пусто / нет слоёв: `,`.
  Не boolean `layer_<id>`.
- Cairo: non-backbone снизу. Геометрический LOD: pixel-identical не обещаем;
  общий `tol(z)` только MVT↔Cairo; none — полная геометрия; паритет по составу.

Диапазоны и coverage пишутся в манифест формата. Расширение zoom — после оценки
диска для **двух** retained на формат + staging. z вне native min/max → 422;
`overzoom` без файла — клиентский overzoom; `missing` в ready → 503.

На первом этапе нет: render в FastAPI, dirty-tile инкремента, raster-вариантов
под фильтры, автозапуска bake из sync, дописывания опубликованного выпуска
через `worker_fill`, интеграции `block_complete` с админкой.
Файлы и манифест опубликованного выпуска неизменяемы; расширение coverage — новый выпуск.

## Поток данных и модули

```text
Userside → map_sync → неизменяемый dataset в PostGIS
                 ├→ map_api → объекты (none + точки)
                 └→ map_tiles (ручной CLI)
                       ├→ pipeline vector → MVT → validate → publish active/prev
                       └→ pipeline raster → WebP → validate → publish active/prev
                              ↓
              метаданные (Bearer/session) + XYZ (UUID±HMAC, проверка БД)
                              ↓
                         Nuxt / внешние сервисы
```

FastAPI и pipeline не ходят в Userside. Сборка читает `map_data.features`.

Модули:

- Общие правила отбора/стиля кабелей — object API и оба pipeline.
- `backend/map_tiles`: планирование, MVT, Cairo/WebP, validate, publish, cleanup,
  CLI (`map-tiles vector …`, `map-tiles raster …` раздельно).
- `backend/map_api`: catalog, TileJSON, style, details; раздача XYZ с проверкой
  статуса версии (и опционально HMAC). Не Bearer на каждый файл.
- `frontend/modules/map-data`: каталоги, details, pin dataset для тайловых режимов.
- `frontend/modules/cartography`: режим, выпуск, MapLibre-адаптер.

Приложение не импортирует MapLibre. Тайлы грузит движок по template из каталога.

## Единые правила содержимого

Сборка формата фиксирует `source_system`, `map_id`, `dataset_id`, cableTypeId,
версию правил и параметры оформления в начале запуска.

Во всех режимах одинаковы скрытие, типы и min_zoom. Семантика `cable_group`:
условия к объекту трассы; рисуются все части. Тематики и стиль — у опубликованного
объекта. Не фильтровать части трассы при нарезке.

Пороги min_zoom 1–14; в сборке уровня `min_zoom <= z`; с z14 — весь разрешённый
набор. Паритет **состава** с none — на порогах zoom (множества objectId).

Тематические фильтры **vector** — на клиенте (`layerIds`). Пустой фильтр = все,
несколько — OR. Серверные ограничения видимости запекаются в выпуск.

Raster: все разрешённые кабели, без тематических фильтров кабелей; точки могут
фильтроваться. UI: нет фильтров/кликов/dash. Каталог:
`rasterCapabilities: { filters: false, pick: false, dashes: false }`.
Toggle видимости слоя кабелей — во всех режимах.

## Геометрический LOD и паритет формы

Два уровня паритета не смешивать:

1. **Состав (жёсткий):** одни правила отбора и `min_zoom` в none / vector / raster.
   Сравнивать входные objectId до simplify/clip на одном dataset, bbox и zoom.
   Vector сравнивается после одинакового тематического фильтра; raster — с none
   без тематического фильтра кабелей. WebP не содержит objectId: для raster
   использовать диагностический набор входных объектов renderer.
   При проверке декодированного MVT объединять фрагменты по objectId и учитывать
   buffer/видимую область; дополнительные объекты за экраном не являются дефектом.
   Отдельно проверять, что simplify/encode не теряет короткие объекты и ветви.
2. **Форма линии (мягкий):** none отдаёт **полную** геометрию (как object API,
   без tile-simplify). MVT и Cairo используют **общий** канон
   `tol(z)` метров в 3857 (предпочтительно один `ST_Simplify` до encode/stroke).
   Таблица `zoom→meters` и её hash входят в манифест и hash параметров выпуска.
   На высоких z `tol → 0`. Vertex-budget preload (если есть у none) в тайлы
   не переносится.

Неизбежные расхождения формы (не баг):

- MVT квантует в extent 4096;
- Cairo растеризует сплошную линию; dash намеренно игнорируется;
- raster overzoom выше prebake несёт упрощение **нижнего** уровня.

Приёмка формы: визуальная узнаваемость трассы; short-cable / ветви fixtures не
теряются; **попиксельный diff не gate**. Не сравнивать overzoom raster с none
на высоком z — с native того же prebake.

## Нарезка, геометрия и оформление

План по пересечению geom с сеткой и индексами; пустые на диск не пишем.
Buffer на границе тайла. MVT: общий simplify по `tol(z)`, затем
`ST_AsMVTGeom` / `ST_AsMVT` в 3857. Упрощение не теряет короткие кабели и
идентичность. Проверки: границы тайла, antimeridian, предельные широты.

Source-layer: `cable_line`. Атрибуты фрагмента:

- стабильный `objectId` (`cable:<id>` / `cable_group:<id>`), sourceKind/sourceId;
- цвет, ширина, штриховка (для vector), порядок и нужные скаляры;
- тематики: **`layerIds`** = `,id,id,…,` (см. решения выше); каталог перечисляет
  id слоёв карты для UI.

`promoteId: objectId`; фрагменты одной трассы — один ID на всех тайлах/уровнях.

Vector: MapLibre-стиль из `cable-style.ts` (solid/dashed без фейкового dash у
solid). Raster: Cairo читает канон без dash:

`geom 3857 → тот же tol(z), что и MVT → stroke solid (non-backbone снизу) → WebP 1x/@2x`.

Cairo рисует в image surface; WebP кодируется отдельной библиотекой, выбранной
на этапе 2 (например Pillow с libwebp). Зафиксировать версии и параметры кодека,
преобразование Cairo premultiplied alpha в RGBA и одинаковую логическую ширину
в 1x/@2x. Проверить отсутствие темных ореолов на прозрачных краях.

Смена раннера — только правкой этого документа до полной raster-сборки.

## Покрытие zoom и манифест

Манифест **каждого формата**:

- `minzoom`, `maxzoom` — полностью готовый native-диапазон;
- `displayMaxzoom` — рекомендуемый предел увеличения карты;
- таблица / hash геометрического `tol(z)` (общий канон MVT↔Cairo);
- raster: `prebakeMaxzoom`, равный native `maxzoom`, политика `overzoom`;
- для native-тайлов — план `prebaked` и диагностика `missing`;
  выше native maxzoom — политика `overzoom` на диапазон, без перечисления всех
  виртуальных потомков в манифесте.

| coverage | Диск | API |
| --- | --- | --- |
| `prebaked` | файл обязателен | отдать; нет файла → 503 |
| `overzoom` | файла нет | клиент overzoom выше source.maxzoom; прямой GET файла → 422 |
| `missing` | дефект | не публиковать; в ready → 503 |

Пустой (нет геометрии) ≠ `missing`: нет в плане или прозрачный WebP / пустой MVT.

TileJSON и MapLibre source используют стандартный `maxzoom` как максимальный
уровень доступных файлов. Выше MapLibre увеличивает последние тайлы. Поле
`maxNativeZoom` не используется: это не настройка источника MapLibre.
`displayMaxzoom` передается отдельным полем каталога, не подменяет source.maxzoom.

`worker_fill` — будущая фаза. До ее включения нужны отдельный протокол coverage,
транзакционный outbox, повышение native maxzoom только после проверки целого
уровня, уведомление клиентов и защита записи относительно publish/retire.
Эти механизмы не входят в первую реализацию.

## Хранение и модель БД

Версия формата — UUID. Dataset и tile-версия раздельны. Один dataset может дать
несколько выпусков каждого формата.

```text
TILES_ROOT/
  <mapId>/vector/.staging/<version>/...
  <mapId>/vector/<version>/<z>/<x>/<y>.pbf
  <mapId>/raster/.staging/<version>/...
  <mapId>/raster/<version>/<z>/<x>/<y>.webp
  <mapId>/raster/<version>/<z>/<x>/<y>@2x.webp
```

Directory listing на `TILES_ROOT` (и раздаче тома) **запрещён** — только
приложение/API по известным path; reverse-proxy не отдаёт autoindex.

| Сущность | Назначение |
| --- | --- |
| `tile_builds` | Запуск **одного** формата: lock/heartbeat, параметры, этап, ошибка |
| `tile_versions` | UUID, format, source/map/dataset, состояния, пути, zoom/coverage, hash, манифест, published_at |
| `active_tile_versions` | Указатели **active** и **prev** на `(source, map, format)` |
| `tile_access_tokens` | Хеш, интеграция, карты, scopes метаданных/details, срок, отзыв |

Состояния: `building → validated → ready → retired → deleted`; до publish —
`failed`. `deleted` — tombstone для 410. Cleanup файлов: pending/done/error.

Защита dataset: активные/prev выпуски обоих форматов, текущие builds,
query_leases. Failed/dead worker отпускает защиту после проверки.

## Публикация: active + prev на формат

Лимит: для `(source_system, map_id, format)` максимум **две** доступные успешные
версии — active и prev. Staging не считается доступной версией.

Pipeline **одного** формата:

1. Восстановить cleanup; взять exclusive lock на `(map, format)`.
2. Зафиксировать ready-dataset; защитить от cleanup.
3. План → сборка в `<mapId>/<format>/.staging/<version>/` → манифест.
4. Validate (полнота `prebaked`, decode, размеры, прозрачность).
5. Перенос в `<mapId>/<format>/<version>/` на том же томе (ещё не active).
6. Транзакция БД: новый → ready+active; бывший active → prev; бывший prev →
   retired.
7. Удалить retired-каталог этого формата; статусы; освободить dataset при возможности.

Rollback: переключить active на prev (тот же format) под тем же lock.
Удалённый выпуск не восстановить указателем. После rollback каталог отдаёт
новый active (= бывший prev) и его **`datasetVersion`**; клиент пинит точки
к этому dataset, не к `active_datasets` и не к устаревшему указателю.

БД и FS не одна транзакция: кратковременно на диске могут быть active+prev+staging
(+ retired до GC). Ошибка GC → `cleanup_pending`, новая сборка формата блокируется
до успеха. Сбой до publish не меняет active. Cleanup идемпотентен.

Параллельно: vector-сборка карты и raster-сборка той же карты — разные locks
`(map, format)`; две vector-сборки одной карты — нельзя.

## API, авторизация и кэш

### Статус версии: Redis + БД

Перед раздачей XYZ API резолвит `version → status` (и mapId/format) через
**Redis** (`TILES_VERSION_STATUS_TTL_SECONDS`, ориентир 30–60 с). Miss →
Postgres → запись в Redis. Invalidate (delete key) при publish / rollback /
retire / delete. При недоступном Redis — fallback на БД (деградация
латентности, не отказ раздачи).

Гарантия первой версии: status cache может устареть не более настроенного TTL.
Один invalidate не устраняет гонку Miss → чтение ready → retire/invalidate →
запись старого ready. Не обещать мгновенный 410 для всех запросов.
При отсутствии ожидаемого файла после cache-hit API повторно проверяет статус
в БД: retired/deleted → 410; ready с потерянным файлом → 503. Не заменять
пропавший ожидаемый файл пустым тайлом. Ошибки не кэшировать как успешные тайлы.

### Режим A (default): UUID + статус

1. Catalog (auth) → `version`, templates без query (или с HMAC в режиме B).
2. XYZ без Authorization.
3. Статус ready (active или prev) → файл по манифесту; retired/deleted → 410.
4. Кэш ответа XYZ: длинный `max-age` / immutable.

Режим A — доступ по известной ссылке, а не авторизация конкретного потребителя.
Любой обладатель URL может читать XYZ; отзыв Bearer прекращает получение
метаданных/details, но не отзывает ранее выданный XYZ URL. Запрет autoindex
не меняет этой модели. Для закрытых интеграций использовать режим B либо
отдельно реализованный транспорт Bearer на XYZ.
Уже загруженные в браузер/CDN тайлы могут оставаться доступными после retire.
Лимит active+prev относится к origin, а не к копиям на клиентах и в CDN.

### Режим B (опция): HMAC на template / path-prefix

Подписывается **префикс** шаблона, например path до `/{z}/{x}/{y}.ext` плюс
`exp`, не каждый тайл отдельно. Catalog отдаёт один signed template на source;
MapLibre подставляет z/x/y. Проверка: HMAC(prefix+exp) и `exp` не истёк, затем
статус версии. TTL часов–сутки; refresh каталога → новый template. Secret на
API. Retired → 410 при валидной подписи.

Подпись охватывает канонический prefix с mapId/format/version и exp, а не
переданную клиентом произвольную строку. Catalog возвращает `templateExpiresAt`;
клиент обновляет шаблон до expiry независимо от poll=0. После возобновления
вкладки expiry проверяется немедленно. Истекшая подпись → 403 с кодом
`TILE_SIGNATURE_EXPIRED`, неверная → 403 `TILE_SIGNATURE_INVALID`.
Первая ошибка expiry запускает единый refresh; invalid не повторяется бесконечно.

Каталог приватный с revalidation. ETag учитывает выданные templates и expiry:
при обновлении подписи неизменный tileVersion не является основанием для 304.
В B max-age XYZ не превышает оставшееся время подписи; для CDN проверка подписи
выполняется перед выдачей cache-hit либо используется revalidation через origin.
Нельзя обходить проверку подписи через публичный кэш. Отзыв metadata-токена
не отзывает действующую HMAC-ссылку до ее expiry.

### Media types (фиксировано)

| Ответ | `Content-Type` | Прочее |
| --- | --- | --- |
| MVT (в т.ч. пустой тайл) | `application/vnd.mapbox-vector-tile` | тело — валидный PBF; **`Content-Encoding: gzip`** |
| WebP 1x / `@2x` / прозрачный пустой | `image/webp` | без gzip на v1 |
| Ошибки JSON метаданных | `application/json` | как map-api |

`Accept` на XYZ не обязателен; при наличии клиент может присылать
`Accept: application/vnd.mapbox-vector-tile` / `image/webp` — сервер не
отклоняет запросы без Accept. Пустой MVT: минимальный валидный tile с пустым
слоем `cable_line` (не zero-byte и не HTML). Пустой raster: кэшируемый
прозрачный 256×256 (и 512 для `@2x`) WebP.

### Контракт

| Метод и адрес | Auth | Назначение |
| --- | --- | --- |
| `GET /api/maps/{mapId}/tiles/catalog` | Bearer/session | Active **и** prev по форматам: version + **datasetVersion** каждого, lag, templates, capabilities |
| `GET /api/maps/{mapId}/tiles/{format}/{version}/tilejson.json` | Bearer/session | TileJSON |
| `GET /api/maps/{mapId}/tiles/vector/{version}/style.json` | Bearer/session | Стиль vector |
| `GET /api/maps/{mapId}/tiles/{version}/objects/{sourceKind}/{sourceId}` | Bearer/session | Details (+ полная геометрия) |
| `GET /tiles/maps/{mapId}/{format}/{version}/{z}/{x}/{y}.pbf` | A / B | MVT gzip |
| `GET /tiles/maps/{mapId}/raster/{version}/{z}/{x}/{y}.webp` | A / B | WebP 1x |
| `GET /tiles/maps/{mapId}/raster/{version}/{z}/{x}/{y}@2x.webp` | A / B | WebP `@2x` |

Details без queryId. TileJSON: bounds, native min/maxzoom, шаблоны.
Catalog дополнительно содержит displayMaxzoom, raster tileSize, ссылки 1x/@2x
и templateExpiresAt в B. CORS на `/api` и `/tiles`. Токены: CLI, scopes
метаданных/details. Проверка pin/details подтверждает доступ к карте и retained
tileVersion, принадлежность dataset и доступность объекта по правилам выпуска.

## Frontend, pin, lag, холодный старт

`cable_tiles` — env деплоя; смена → **restart**.

- Нет ready active формата → **`none`**. Админ: sync → bake vector **и** raster
  на том же dataset → выставить env → restart.
- Runtime-ошибка тайлов → ошибка в UI, без auto → `none`.

`none`: preload/batch, `active_datasets`. `vector|raster`: кабели вне preload;
точки pinned к **datasetVersion активного** выпуска выбранного формата (после
rollback — к datasetVersion нового active = бывший prev).

Каталог отдаёт по каждому формату active и prev: `tileVersion`,
`datasetVersion`, templates; плюс `datasetAge`, `tilesBehindActive`,
`tiles_lag`. Default UI: `banner`. `block_complete` отложен до будущей админки.

Слои MapLibre: basemap → **кабели** (vector или raster) → **узлы/муфты** → UI
оверлеи (подсветка выбора, measure). Vector: клик → details. Raster: без
hit-test; `@2x` по DPR. Measure без snap.

Catalog: ETag; focus/visibility; 410 → refresh; poll default 0.
В B отдельный таймер обновляет HMAC до templateExpiresAt даже при отключенном
poll; expiry → refresh с ограниченными retries/backoff. Смена только подписи
обновляет transport templates, не сбрасывает dataset/выбор. Смена версии
отменяет прежнее поколение запросов и согласованно меняет точки/тайловый источник.

## Эксплуатация и приёмка

- Bake: ручные CLI с одинаковым `--dataset <UUID>`; sync не запускает.
- Алерт отставания после периода ожидания `TILES_LAG_GRACE_SECONDS`; failed bake
  сообщает ошибку сразу. Период ожидания выбрать по замерам полного цикла.
- Redis для кэша статуса версий; reverse-proxy: TLS, CORS при необходимости,
  **без autoindex** на TILES_ROOT, лимиты/периметр на `/tiles` — описать в
  гайде интеграции (этап 10).
- Настройки (`.env.example`): `cable_tiles`, TILES_ROOT, public URL, zoom,
  `tiles_lag` (default `banner`), `TILES_CATALOG_POLL_SECONDS` (default `0`),
  Redis URL/TTL, `TILES_XYZ_SIGNING`, HMAC secret/TTL, concurrency, CORS,
  TILES_LAG_GRACE_SECONDS. `block_complete` не включать в рабочие настройки v1.
- Том общий; API read-only; Cairo и WebP-кодировщик в worker; listing запрещён.

Приёмка:

1. Входные objectId на одном dataset/bbox/zoom; vector с одинаковым фильтром,
   raster без фильтра кабелей; MVT проверяется с учетом buffer, WebP — визуально.
2. Форма узнаваема; не pixel-identical; общий `tol(z)` MVT↔Cairo.
3. Short-cable fixtures; ширины **узнаваемы**, не обязаны совпадать в px
   между none / vector / raster.
4. Pixel-diff не gate.
5. Слои: кабели под точками; gzip MVT; Content-Type WebP/MVT как в таблице.
6. Catalog после rollback отдаёт datasetVersion нового active.
7. ≤2 ready на формат; cold start → none; measure без snap; алерты; recovery.
8. Native maxzoom ≥14 и полный набор коротких кабелей при overzoom;
   source.maxzoom согласован с готовыми файлами.
9. B: refresh до expiry при poll=0, обновление ETag подписей, защита cache-hit.
10. Redis stale-read race укладывается в TTL; пропавший retired файл дает 410
    после повторной проверки БД; опубликованные файлы/манифест неизменяемы.

Основания: [PostGIS ST_AsMVTGeom](https://postgis.net/docs/ST_AsMVTGeom.html),
[MapLibre transformRequest](https://maplibre.org/maplibre-gl-js/docs/API/type-aliases/RequestTransformFunction/),
[MapLibre vector tile source](https://github.com/maplibre/maplibre-gl-js/blob/main/src/source/vector_tile_source.ts),
mobilemap `docs/line-tiles.md` (Cairo/WebP, active+prev).
