# GIS API тайлов: этап 7

2026-10-09. Новые HTTP-маршруты подключены к FastAPI. Backend работает с
retained snapshot выпуска; Bitrix24 не используется. Frontend подключение
и обновление templates относятся к этапам 8–9, production wiring — к этапу 10.

## Токены и настройки

Миграция 010 создаёт `api_tokens` и SECURITY DEFINER `api_pin_tile_dataset`.
API получает SELECT токенов и EXECUTE pin, без UPDATE snapshots/токенов.
Токен — 256 случайных бит; в БД хранится только SHA256 digest, map IDs,
metadata/details scopes, expiry и revoked_at. Plaintext возвращается один раз.

```sh
map-token --env .env create --name gis --map 3 --scope metadata --scope details --ttl-seconds 2592000
map-token --env .env list
map-token --env .env revoke --id <digest>
```

CLI требует отдельный `MAP_TOKEN_ADMIN_DATABASE_URL`, не использует source DSN
и не подставляет API/owner URL автоматически. Bearer передаётся в Authorization;
также принимается cookie `map_tiles_session` с тем же opaque token. Cookie выдаёт
доверенный внешний session layer; endpoint выдачи cookie в этой фазе отсутствует.
При заданном Authorization он имеет приоритет, invalid header не обходится cookie.
401 — invalid/revoked/expired, 403 ACCESS_DENIED — scope/map mismatch.
Уже выданные XYZ URL не отзываются revocation metadata-token.

| Переменная | Default |
| --- | --- |
| `TILES_ROOT` | `./data/tiles` |
| `TILES_PUBLIC_BASE_URL` | `http://localhost:8000` |
| `MAP_API_SOURCE_SYSTEM` | `userside` |
| `MAP_REDIS_URL` | отсутствует: DB fallback |
| `TILES_VERSION_STATUS_TTL_SECONDS` | `30` |
| `TILES_XYZ_SIGNING` | `none` (A); опция `hmac` (B) |
| `TILES_HMAC_SECRET` | обязательна в B, минимум 32 символа |
| `TILES_SIGNATURE_TTL_SECONDS` | `86400` |
| `TILES_LAG` | `banner`; block_complete отложен |
| `TILES_CATALOG_POLL_SECONDS` | `0` |

Public base должен быть доверенным абсолютным http(s) URL. Host header клиента
не участвует в выдаче templates. В production используется HTTPS через proxy.
CORS действует на /api и /tiles: явные origins, Authorization/If-None-Match,
exposed ETag/Content-Encoding. Cookie credentials включаются только при конкретных
origins; wildcard не включает credentials. Ошибки имеют JSON envelope и no-store.

## Catalog, TileJSON, style, details

- `GET /api/maps/{mapId}/tiles/catalog`: metadata scope. Active/prev по каждому
  формату со своим datasetVersion, layers, datasetAge, tilesBehindActive,
  capabilities и templates/expiry. Cold start: null active/prev, корректный 200.
  Чтение catalog — repeatable-read transaction; release pointers согласованы.
- `GET /api/maps/{mapId}/tiles/{format}/{version}/tilejson.json`: metadata scope.
  Native maxzoom, bounds, XYZ scheme, tileSize 512 vector / 256 raster;
  raster @2x template, vector_layers с scalar fields.
- `GET /api/maps/{mapId}/tiles/vector/{version}/style.json`: style v8, promoteId
  objectId, source-layer cable_line, solid/dashed, min_zoom и renderOrder.
  Тематические фильтры по `,id,` подключит UI через общий style helper.
- `GET /api/maps/{mapId}/tiles/{version}/objects/{sourceKind}/{sourceId}`: details
  scope, retained version, полная geometry snapshot/style/properties/links.
  Cable whitelist берётся из immutable manifest этого выпуска; hidden и
  неподходящий тип не доступны. Point providers берутся из общего registry.
  queryId не требуется. Details используют repeatable-read snapshot, чтобы
  concurrent publish/cleanup не менял данные между проверкой выпуска и чтением объекта.

Metadata private/no-cache, Vary Authorization/Cookie. ETag хэширует всё тело,
включая signature и templateExpiresAt; авторизация выполняется до проверки ETag.
При обновлении подписи прежнего UUID нельзя возвращать ошибочный 304.
Dataset age дан в целых секундах; его изменение также меняет body/ETag.

## Pin и объектный режим

POST `/api/map/objects/preload` принимает необязательный
`pin: {tileVersion, format: vector|raster}`. Metadata scope и доступ к карте
обязательны при pin. Произвольный dataset клиент не задаёт: сервер выводит его
из retained release и закрепляет snapshot через SECURITY DEFINER под share lock.
Query lease записывает pin, кабели из objectTypes удаляются, точки отбираются
по прежним bbox/zoom/layer правилам. Pin с одним cable_line даёт пустую query.
Pinned batch также требует metadata scope; legacy query details для pinned query
требует details scope. Revocation прекращает эти запросы.

None не передаёт pin, использует active_datasets и сохраняет прежнее поведение
объектного API. После rollback catalog сообщает datasetVersion бывшего prev,
а pin выбирает его, даже если active_datasets уже новее. tokens не меняют
прежнюю политику доступа legacy none API; закрытый deploy должен ограничивать
legacy /api/map периметром proxy/доверенным frontend server.

## XYZ, status cache и ошибки

`GET /tiles/maps/{mapId}/{format}/{version}/{z}/{x}/{y}.pbf|webp`; raster @2x:
`{y}@2x.webp`. Валидируются enum, UUID, XYZ, extension/density. Только
канонические файлы под release root; StaticFiles/listing не подключены.

Async Redis hit → малая status metadata → immutable manifest index → файл.
Hit не берёт DB connection. Miss/Redis down → SELECT status → SETEX. Manifest
LRU ограничен 8 выпусками, SQL manifest не читается на hot path. Файлы читаются
в threadpool; size/SHA256 проверяются. После отсутствия/повреждения ожидаемого
файла либо manifest status повторно читается из БД, bypass Redis: retired/deleted
→ 410 TILE_VERSION_RETIRED; ready → 503 TILE_FILE_MISSING. Пустой тайл не
подменяет missing. Stale ready после cache-fill race допускается до TTL.

Нет записи в sparse native coverage → валидный empty cable_line MVT либо
transparent WebP 256/512. WebP empty предкодированы: API не требует Cairo/Pillow.
Выше native — 422 TILE_OVERZOOM, применяется client overzoom. Неизвестный
UUID/map/format — 404 TILE_VERSION_NOT_FOUND. Ошибки no-store.

MVT всегда имеет Content-Encoding gzip и MIME application/vnd.mapbox-vector-tile,
включая empty; на диске raw PBF. WebP MIME image/webp, без gzip. XYZ ETag от
wire bytes; авторизация/signature/status проверяются до 304.

## Модели доступа A/B

A (`none`): XYZ без Bearer, public/max-age=31536000/immutable. Доступ по известной
ссылке; metadata-token не отзывает URL. Retire ограничивает origin, не уже
загруженные копии в browser/CDN.

B (`hmac`): один HMAC SHA256 для канонического prefix
`/tiles/maps/{mapId}/{format}/{version}` + newline + exp. Catalog выдаёт signed
source template; raster 1x/@2x используют ту же подпись. Координаты подставляет
MapLibre/GIS, сервер не подписывает каждый XYZ. Signature INVALID/EXPIRED —
разные 403 codes; неверный map/format/version меняет prefix и ломает подпись.

B browser max-age не превышает остаток подписи. Ответ дополнительно задаёт
`s-maxage=0, must-revalidate`: shared CDN обязан revalidate через origin даже
на cache-hit либо самостоятельно проверять signature перед hit. Конфигурация
CDN не должна игнорировать эти требования. Signature проверяется до 304;
expired URL не превращается в cached 304. Обновление templates до expiry при
poll=0, focus/visibility и единый refresh реализуются на этапе 9.

## Проверки

Disposable PostGIS + Redis и настоящие prebaked файлы: auth/cold start/scopes,
revocation/expiry/cookie, CORS/ETag, catalog/TileJSON/style, raw ASGI gzip + MVT
decode, WebP 1x/@2x/empty, native/overzoom/coordinates/path safety, details/group/
hidden/type whitelist, signed prefix/densities/expiry/renewed ETag/cache bound,
no-DB hot hit и Redis-down fallback, stale ready → 410/ready missing → 503,
HTTP rollback datasetVersion/lag и pinned point/batch auth, настоящая роль map_api
без прав записи snapshots/tokens. Token CLI create/list/revoke проверен отдельно,
plaintext не сохранён в reports. Docker API собран без Pillow/Cairo.

131 backend tests passed, 0 skipped, 167.79s. После финальных изменений —
12 API/HTTP tests passed, затем 9 окончательных tile API gates passed (26.62s).
Ruff passed; Docker API build/smoke и token CLI passed. Этап 7 принят.

Результаты regression и окончательная приёмка этапа: cable-tiles-validation.md.
Следующие этапы: frontend modes, signed template refresh, deploy/proxy/ops guide.
