# План разработки тайлов кабелей

Дата: 2026-10-07, уточнено 2026-10-08. Основание:
[архитектура решения](cable-tiles-architecture.md).
Текущий прогресс реализации и доказательства проверок записываются в
[cable-tiles-validation.md](cable-tiles-validation.md). Наличие отдельных
модулей не означает приёмку всех этапов.
Этапы выполняются последовательно. Каждый следующий этап начинается после
проверки результата предыдущего. Рабочий объектный режим сохраняется на всем пути.

## Этап 1. Контракты и контрольные данные

1. Сопоставить SQL отбора в `backend/map_api/app.py`, сборку в `map_sync`,
   frontend-контракты и текущие стили.
2. Выделить общие правила видимости и render-атрибутов; подключить к API без
   смены поведения.
3. JSON-контракты: catalog (active+prev с datasetVersion каждого, lag,
   `tiles_lag`, ETag, templates A/B path-prefix HMAC); TileJSON
   (native maxzoom, prebake, `@2x`), catalog displayMaxzoom/templateExpiresAt;
   media types (gzip MVT, WebP); отдельные ошибки expiry/invalid подписи;
   `layerIds` = `,id,id,…,`; coverage; pin только vector|raster; active+prev;
   `cable_tiles` + restart.
4. Fixtures: короткий/длинный кабель, solid/dashed, multi-part cable_group,
   скрытые части, тематические слои (проверка `layerIds`), граница тайла.
5. Bbox, пороги zoom, ID и эталонные кадры. Паритет по входным objectId:
   vector с одинаковым фильтром, raster без фильтра кабелей; MVT с учетом buffer.
   Для raster использовать диагностический набор входа renderer, не ID из WebP.

Результат: общие правила, спецификация контрактов, fixtures.
Проверка: текущие API-тесты; ID/стили совпадают. Лишний рефакторинг не включать.

## Этап 2. Прототип тайлов и Cairo raster

1. MVT на контрольном dataset (buffer, objectId, `layerIds`).
2. Прототип vector в MapLibre: source.maxzoom, min_zoom, фильтры
   по `layerIds` (`,id,`), кабели **под** точками, фрагменты трасс.
3. MapLibre-стиль vector (solid/dashed).
4. Cairo image surface → отдельный WebP-кодировщик → 1x+`@2x`, solid-only,
   non-backbone снизу, Docker worker. Выбрать библиотеку/параметры кодека,
   проверить premultiplied alpha → RGBA и прозрачные края.
5. Замеры zoom → выбрать `vector_maxzoom`, `raster_prebake_max`,
   `displayMaxzoom`, buffer, общую таблицу `tol(z)` для MVT и Cairo, concurrency.
   Native-предел обоих форматов ≥14; старт z14, z15 и выше по замерам.
   Стратегия v1: полный prebake + клиентский overzoom, без worker_fill.
   Проверить соответствие zoom карты и XYZ для vector и raster tileSize 256.
6. Записать в `docs/cable-tiles-prototype.md` (включая hash/канон simplify).

Результат: MVT↔vector и geom→Cairo→WebP; zoom/coverage/`tol(z)` зафиксированы.
Проверка: transparent; dash только в vector; швы; overzoom; `@2x`; short-cable
fixtures живы на native и overzoom; входной состав совпадает с none по правилам
этапа 1; без pixel-diff gate. Без публикации
active. Без этого этап 5 не начинать.

## Этап 3. Миграции и жизненный цикл

1. Миграции: `tile_builds`, `tile_versions` (с `format`), `active_tile_versions`
   (active+prev на `(source,map,format)`); ограничения, индексы, FK.
2. Exclusive lock на `(map, format)`; heartbeat; dead-worker recovery.
3. Защита dataset от cleanup для active/prev обоих форматов и текущих builds.
4. Tombstones и статусы файловой очистки.
5. Порядок блокировок sync / cleanup / tile publish.
6. SQL grants: API читает выпуски; worker пишет сборку/publish своего формата.
7. Redis: ключи `version→status`, TTL, invalidate на publish/rollback/retire.

Результат: модель lifecycle active+prev на формат + Redis status cache.
Проверка: миграции; cleanup; locks; Redis miss→DB→set; invalidate;
гонка cache fill с retire (устаревание ограничено TTL); Redis down → DB.
Disposable PostGIS (+ Redis).

## Этап 4. Полная vector-сборка (отдельный pipeline)

1. Пакет `backend/map_tiles`, CLI `map-tiles vector --dataset <UUID>`, конфиг.
2. Проверка явного dataset (ready/карта), фиксация правил/hash в начале.
3. План minzoom…`vector_maxzoom`; манифест `prebaked`; staging
   `<mapId>/vector/.staging/<version>/`.
4. MVT: `tol(z)`, buffer, objectId, стиль, `layerIds=,…,`; gzip при отдаче
   на этапе 7 (на диске — сырой PBF или уже gzip — зафиксировать в манифесте;
   предпочтительно сырой на диске, gzip в API).
5. Validate, отмена/повтор; hash `tol` в манифесте.
6. Отчёт по zoom.

Результат: vector staging + отчет, без публикации (она реализуется на этапе 6).
Проверка: fixtures, cable_group, границы, короткий объект; состав vs none;
не побайтовый/попиксельный паритет с GeoJSON.

## Этап 5. Полная raster-сборка (отдельный pipeline)

1. CLI `map-tiles raster --dataset <UUID>`; Cairo и выбранный WebP-кодировщик
   в worker image. Dataset явный и совпадает с vector в операционном сценарии.
2. Полный prebake WebP 1x+`@2x` до `raster_prebake_max` ≥14; выше — только
   клиентский overzoom. После публикации файлы/манифест не дописываются.
3. Тот же `tol(z)`, что vector; buffer по ширине; non-backbone снизу.
4. Лимиты процессов/памяти.
5. Манифест: `prebaked` (обе плотности); `overzoom` без файла; `missing` →
   не publish; hash `tol`.
6. Визуал: узнаваемость vs none/vector; former-dashed → solid; short-cable;
   без pixel-diff gate.

Результат: raster validated независимо от vector.
Проверка: validate, alpha/края и одинаковая логическая ширина 1x/@2x.
На этапе 6 проверить независимость публикаций и ошибок форматов.
Сам этап 5 не публикует выпуск.
Замер диска: 2 retained × формат + staging + `@2x`.

## Этап 6. Публикация active+prev и восстановление

1. Publish по формату: staging → final → active; old active → prev; old prev →
   retired + delete.
2. Rollback на prev; invalidate Redis; каталог отдаёт datasetVersion нового
   active.
3. Идемпотентный cleanup; вызов в начале/конце pipeline формата.
4. `cleanup_pending` блокирует следующую сборку **этого** формата.
5. Опубликованные файлы и манифест неизменяемы; расширение coverage — новый выпуск.
6. Инъекции сбоя rename/commit/GC.

Результат: ≤2 ready-каталога на формат.
Проверка: active+prev; независимость публикаций/ошибок форматов; повтор cleanup;
datasetVersion после rollback; отчет publish vs cleanup. HTTP-каталог с этой
семантикой проверяется на этапе 7.

## Этап 7. API: метаданные, XYZ A/B, кэш

1. Токены метаданных/details; CLI.
2. Catalog/TileJSON/style/details: active+prev с datasetVersion; Bearer/session.
3. XYZ: Redis→status→файл; режим A; опция B = HMAC **path-prefix/template**.
   CORS `/api`+`/tiles`. Content-Type/gzip как в архитектуре.
4. Манифест → коды ошибок; native source/TileJSON maxzoom и catalog displayMaxzoom.
   Ожидаемый файл пропал после Redis hit → повторная проверка БД (410 или 503).
5. Details без queryId.
6. Pin preload к datasetVersion выпуска только вне none; lag; `tiles_lag`.
7. ETag учитывает templates/expiry; templateExpiresAt; expired/invalid signature
   имеют отдельные коды. В B ограничить max-age сроком подписи и проверять
   подпись на CDN cache-hit либо revalidate через origin.
8. Immutable XYZ; path traversal; без listing. Документировать режим A как
   доступ по ссылке, отсутствие отзыва XYZ через Bearer и жизнь клиентских кэшей.

Результат: auth метаданных; XYZ с Redis; gzip MVT; WebP types.
Проверка: 401 catalog; XYZ без Bearer; 410; gzip; HMAC prefix в B; rollback
catalog datasetVersion; pin только vector|raster; signature expiry/invalid;
обновление подписи при прежнем tileVersion дает свежий template, не ошибочный 304;
Redis stale-ready + retired файл → 410 после повторной проверки БД.

## Этап 8. Frontend: none и vector

1. `cable_tiles` из env; документировать необходимость restart.
2. Нет vector active → none (админ: bake обоих форматов после sync).
3. Catalog ETag/lag; details через Nuxt proxy; XYZ напрямую.
4. Vector source под точками; стиль; promoteId; фильтры `index-of` по
   `layerIds`; toggle слоя.
5. Pin к datasetVersion active выпуска; кабели вне preload.
6. Клик → details → карточка/подсветка поверх.
7. Measure без snap. UI-баннер при `tiles_lag=banner`.

Результат: none/vector; cold start; lag banner.
Проверка: фильтры `,id,`; кабели под узлами; measure без snap; none без pin.

## Этап 9. Frontend: raster и обновление выпусков

1. Raster WebP, tileSize, source.maxzoom и displayMaxzoom, 1x/`@2x` по DPR.
2. Без фильтров/hit-test/dash; UI предупреждения.
3. Catalog: ETag, focus/visibility, poll из конфига.
4. 410 → единый refresh. В B обновлять template до templateExpiresAt даже при
   poll=0; expiry → refresh с retries/backoff; invalid не зацикливать.
   Новая подпись того же выпуска не сбрасывает dataset/выбор.
5. Смена version: cancel, сброс выбора, точки+источник.
6. Ошибки явно; без auto-fallback на none.

Результат: три режима; переход на новый выпуск.
Проверка: retina; 410; focus; разделение datasets по формату; HMAC expiry при
poll=0, возврат из спящей вкладки, обновление ETag и отсутствие request storm.

## Этап 10. Docker, алерты, приёмка

1. Worker+Cairo+WebP-кодировщик; API read-only; Redis; CLI vector/raster раздельно.
2. `.env.example`, compose; ops: после sync оба bake с одним `--dataset <UUID>`;
   sync ≠ tiles. Замер полного цикла задает TILES_LAG_GRACE_SECONDS.
3. Метрики и алерт отставания после grace period; failed bake сообщает сразу.
   `block_complete` отложен, не реализовывать админку в этой фазе.
4. Readiness; логи без секретов; **запрет autoindex** TILES_ROOT.
5. Гайд интеграции **обязан** включить: reverse-proxy (TLS, no autoindex,
   лимиты/периметр `/tiles`), Bearer метаданных, XYZ A/B (HMAC path-prefix),
   media types/gzip, path, TileJSON, `@2x`, 410, ETag, CORS, lag defaults
   (`banner`, poll `0`), active+prev, rollback datasetVersion, ручной bake
   с явным dataset, native maxzoom/displayMaxzoom, обновление HMAC до expiry,
   модель доступа A и ограничения кэша/отзыва, grace period алерта.
6. Замеры; `docs/cable-tiles-validation.md` (в т.ч. ширины узнаваемы ≠ px).

Результат: деплой, ops-алерты, proxy-требования в доке.
Проверка: ≤2 версий/формат; cold start; алерт с grace period; gzip; слои;
три режима; native ≥14/overzoom; защита CDN cache-hit в B; Redis race/recovery.

## Критерий завершения

Все этапы приняты по архитектуре: раздельные CLI с одним явным dataset;
Redis status; gzip MVT; `layerIds=,id,`; кабели под точками; HMAC path-prefix;
catalog datasetVersion после rollback; defaults banner/poll0; restart для
`cable_tiles`; `.env.example`; proxy/no-listing в гайде; ширины узнаваемы;
native maxzoom ≥14, полный prebake + overzoom; выпуски неизменяемы;
refresh HMAC при poll=0; паритет входных ID и grace period алертов.

Старт с этапа 1. На этапе 2 зафиксировать zoom/coverage/Cairo; не возвращать
выбор раннера на финал. worker_fill и block_complete — будущие отдельные фазы.
