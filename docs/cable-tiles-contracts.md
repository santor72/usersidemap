# Контракты тайлов v1

Этап 1, 2026-10-08. Контракты реализованы до HTTP API включительно (этапы 1–7);
frontend/deploy ещё проверяются по последующим этапам. Архитектура и план остаются источником требований.

## Отбор и render-канон

`backend/map_render/cables.py` задаёт общие SQL-предикаты API и worker.
Dataset неизменяемый; источник `cable|cable_group`, тип `cable_line`,
`hidden=false`, `min_zoom<=zoom`. Тип кабеля: scalar `cableTypeId` либо
пересечение строкового массива `cableTypeIds` с разрешёнными ID.
Пустой/отсутствующий фильтр тематик означает все объекты; непустой — EXISTS
в `feature_layers`. Группу выбираем целиком, не режем её части по фильтрам.
Скрытые самостоятельные кабели не выбираются; скрытые части группы сохраняются
по действующим правилам сборки `cable_groups.py`.

API использует ST_Intersects с bbox 4326; при west>east — две области до ±180.
Worker использует буферный envelope XYZ в 3857. Поэтому паритет ID проверяется
по одинаковому bbox с учётом buffer, до MVT-квантования. WebP ID не содержит:
для raster сверяется диагностический состав входа Cairo.

Пороги длины (строго `<`, в метрах): 50→14, 100→13, 250→12, 500→11,
1000→10, иначе 1. `min_zoom` snapshot — авторитетен; worker не пересчитывает.
Стиль snapshot уже учитывает тип, магистраль, число волокон, план,
тематику и индивидуальный цвет. Общие атрибуты:

| Атрибут MVT | Значение |
| --- | --- |
| objectId | `cable:<id>` или `cable_group:<id>`; один для всех фрагментов |
| sourceKind/sourceId | исходный тип и integer ID |
| min_zoom | порог snapshot |
| layerIds | сортированные уникальные ID: `,1,11,`; без слоёв `,` |
| __mapColor/__mapWidth | style.color/width; fallback `#0877d1`/3 |
| __mapDashArray | style.dashArray либо пустая строка |
| renderOrder | 0 обычный, 1 магистраль; Cairo рисует в этом порядке |

Поля с `__map` совместимы с существующим `cable-style.ts`. Solid без dash shader;
vector поддерживает `20,15` и `1,10`. Cairo всегда solid, ширина в логических
пикселях при обеих плотностях. Новые snapshots включают `isBackbone`;
у группы — признак представителя, задавшего стиль. Старые snapshots без этого
поля получают порядок 0: перед рабочим bake нужен sync новой версии.

## Catalog

`GET /api/maps/{mapId}/tiles/catalog`, Bearer/session, scope metadata.
Ответ private/revalidate, ETag от всего тела (включая templates и expiry).
Нет формата — его active/prev равны null. Клиент при cold start переходит в none.
По каждому формату active и prev содержат собственный datasetVersion:

```json
{
  "mapId": 3,
  "tiles_lag": "banner",
  "catalogPollSeconds": 0,
  "activeDatasetVersion": "11111111-1111-4111-8111-111111111111",
  "layers": [{"id": 1, "title": "Район", "defaultVisible": true}],
  "formats": {
    "vector": {
      "active": {
        "tileVersion": "22222222-2222-4222-8222-222222222222",
        "datasetVersion": "11111111-1111-4111-8111-111111111111",
        "minzoom": 1,
        "maxzoom": 14,
        "displayMaxzoom": 24,
        "tileSize": 512,
        "template": "/tiles/maps/3/vector/22222222-2222-4222-8222-222222222222/{z}/{x}/{y}.pbf",
        "template2x": null,
        "templateExpiresAt": null,
        "tilejsonUrl": "/api/maps/3/tiles/vector/22222222-2222-4222-8222-222222222222/tilejson.json",
        "styleUrl": "/api/maps/3/tiles/vector/22222222-2222-4222-8222-222222222222/style.json",
        "datasetAge": 20,
        "tilesBehindActive": false,
        "capabilities": {"layerFilter": true, "hitTest": true, "dash": true}
      },
      "prev": null
    },
    "raster": {"active": null, "prev": null}
  }
}
```

Возраст dataset — секунды от публикации dataset. `tilesBehindActive` сравнивает
dataset выпуска с active_datasets. Rollback меняет active и prev местами,
datasetVersion берётся из выпуска. Raster отличается `prebakeMaxzoom=maxzoom`,
template `.webp`, template2x `@2x.webp`, styleUrl null и все capabilities false.
Native maxzoom обоих форматов ≥14. Выбраны vector 14, raster 15,
displayMaxzoom 18. Raster tileSize=256; vector source в MapLibre всегда 512.

## TileJSON и style

`GET /api/maps/{mapId}/tiles/{format}/{version}/tilejson.json`, metadata auth.
TileJSON 3.0.0: `tiles:[template]`, `scheme:xyz`, `bounds:[w,s,e,n]`,
`minzoom`, `maxzoom` **native**, `tileSize:512` для vector, `256` для raster.
Raster дополнительно `prebakeMaxzoom`, `template2x`; vector `vector_layers`
с единственным id `cable_line` и перечисленными scalar fields.
`maxNativeZoom` не используется. MapLibre source.maxzoom = native maxzoom,
map maxZoom = displayMaxzoom; выше native применяется клиентский overzoom.
При integer zoom карты q vector запрашивает XYZ q, raster 256 — XYZ q+1.
Worker отбирает raster по min_zoom<=z-1, vector по min_zoom<=z.
`GET .../vector/{version}/style.json` отдаёт style v8 с vector source,
`promoteId:objectId`, `source-layer:cable_line`, двумя solid/dashed слоями.
UI вставляет оба слоя под узлами. Filter тематик ищет `,id,` через index-of,
порог min_zoom применяется по zoom карты, включая overzoom.

## Manifest и coverage

Manifest содержит version, datasetVersion, mapId, sourceSystem, format,
rulesVersion/hash, minzoom/maxzoom/displayMaxzoom, bounds, tolerance table/hash,
encoding (`raw-pbf` на диске для vector; `webp` для raster), coverage.
Raster: prebakeMaxzoom, обе плотности. Sparse-план содержит только непустые
XYZ; отсутствие записи внутри native означает геометрически пустой тайл,
а не missing. Native запись `prebaked` требует файла; `missing` запрещает
publish. Overzoom задаётся одним диапазоном `maxzoom+1..displayMaxzoom`
без файлов. Manifest и файлы неизменяемы после publish.

## XYZ и ошибки

`GET /tiles/maps/{mapId}/{format}/{version}/{z}/{x}/{y}.pbf|webp`;
для raster также `{y}@2x.webp`. Координаты: 0≤x,y<2^z.
Пустой MVT — валидный PBF со слоем cable_line, gzip, MIME
`application/vnd.mapbox-vector-tile`. Raster — transparent WebP 256×256,
512×512 для @2x, MIME image/webp без gzip.

| Статус / error.code | Условие |
| --- | --- |
| 401 AUTH_REQUIRED | нет/невалидна авторизация metadata/details |
| 403 ACCESS_DENIED | scope/map не разрешён |
| 403 TILE_SIGNATURE_EXPIRED | корректная подпись, срок истёк |
| 403 TILE_SIGNATURE_INVALID | нет подписи или неверная подпись в B |
| 404 TILE_VERSION_NOT_FOUND | неизвестный UUID/map/format |
| 410 TILE_VERSION_RETIRED | retired/deleted, включая tombstone |
| 422 TILE_OVERZOOM | прямой GET выше native в display-диапазоне |
| 422 INVALID_TILE_COORDINATES | неверные XYZ/плотность/zoom |
| 503 TILE_FILE_MISSING | prebaked/missing запись без готового файла |

Ошибка: `{"error":{"code":"...","message":"..."}}`, без success cache.
Redis status cache проверяет map/format/version; miss→DB→set, outage→DB.
При отсутствии ожидаемого файла после ready cache-hit повтор DB: retired→410,
ready→503. Stale-ready race ограничена TTL, не обещает мгновенный отзыв.

Режим A — URL без Bearer. Metadata-token не отзывает ранее выданные XYZ ссылки.
В B подписывается канонический prefix `/tiles/maps/{mapId}/{format}/{version}`
и exp; один query `?exp=<unix>&sig=<hmac>` на template и @2x. Expiry ISO UTC
в templateExpiresAt; ETag должен меняться при обновлении подписи прежней версии.
Immutable max-age в B ≤остаток TTL; CDN обязан проверять подпись до cache-hit
либо revalidate через origin. Возобновление вкладки и отдельный expiry timer
обновляют каталог даже при poll=0; invalid не запускает бесконечный retry.

## Details и pin

`GET /api/maps/{mapId}/tiles/{version}/objects/{sourceKind}/{sourceId}`,
scope details; retained release; проверяются карта, доступность объекта и
правила выпуска. Возвращает objectId, tileVersion, datasetVersion, полную
геометрию snapshot, style, properties и links; queryId не требуется.

Preload расширяется pin `{tileVersion,format}` только для vector/raster.
Dataset pin выводится сервером из retained release; произвольный dataset
клиенту недоступен. none не отправляет pin и использует active_datasets.
Pin исключает кабели из preload, согласует точки и выпуск. Смена только
template не меняет pin/выбор; смена версии отменяет прошлое поколение.
Настройка env `cable_tiles=none|vector|raster` требует restart.

## Контрольные данные и ворота

`backend/tests/fixtures/cable_tiles.json`: короткий кабель, магистраль, два dash,
скрытый кабель, multipart group, запрещённый тип, граница z14, antimeridian,
предельная широта. Не-пространственные ожидаемые ID зафиксированы отдельно.
PostGIS-тест сравнивает прежний SQL, общий SQL и реальные HTTP preload/batch
на одинаковых bbox/zoom/слоях. Тесты сборки групп проверяют скрытые/ошибочные
части. Существующий frontend style-test проверяет solid/dashed expressions.
Декодированные MVT ID, diagnostic Cairo inputs и визуальные native/overzoom/retina
проверены на этапе 2; lifecycle/pipeline/API gates — в cable-tiles-validation.md.
