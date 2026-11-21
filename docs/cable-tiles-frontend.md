# Nuxt: none/vector/raster (этапы 8–9)

Этапы 8–9 реализованы и проверены. Эксплуатационный Compose, метрики, алерты
и общая приёмка относятся к этапу 10.

## Настройка

`NUXT_PUBLIC_CABLE_TILES=none|vector|raster` задаёт начальный режим при запуске Nuxt.
`CABLE_TILES` поддерживается как значение по умолчанию при чтении Nuxt config
(dev/build), если нет `NUXT_PUBLIC_CABLE_TILES`. Для готового Nitro build используйте
`NUXT_PUBLIC_CABLE_TILES`. Смена требует restart процесса; для изменения встроенного
значения по умолчанию — rebuild. Не меняйте настройку через hot reload.

`none` сохраняет прежний объектный протокол без pin и запросов tile catalog.
При `vector|raster` панель позволяет переключаться между всеми тремя режимами.
Если при старте нет опубликованного выбранного формата, карта остаётся в none с объяснением. Ошибка metadata
не считается холодным стартом: показывается ошибка запуска и кнопка повтора.
Потеря active при обновлении не переключает карту автоматически в none.

`NUXT_MAP_TILES_TOKEN` — серверная переменная Nitro: токен CLI со scopes
`metadata` и `details`, разрешённой картой из `NUXT_PUBLIC_MAP_ID`. Она не попадает
в public runtimeConfig, HTML или XYZ. `NUXT_MAP_API_INTERNAL_URL` — адрес FastAPI
для серверного proxy. Без сервисного токена proxy передаёт Bearer/session клиента.
При сервисном токене Nuxt открывает метаданные этой карты всем, кто допущен к
приложению; собственный периметр Nuxt нужен для закрытой карты.

Новый proxy `/api/maps/...` разрешает только GET metadata/details для карты
деплоя, передаёт ETag/304. Существующий `/api/map/...` также передаёт credential
для pinned preload и batch. XYZ идут напрямую на абсолютный template из catalog;
`TILES_PUBLIC_BASE_URL` должен быть доступен браузеру и иметь соответствующий CORS.
Compose подключает эти env, RO tile volume для API и отдельные ручные workers.
Развёртывание описано в [эксплуатационном гайде](cable-tiles-operations.md).

## Поведение

- `MapTileClient` получает catalog с If-None-Match и принимает новую подпись
  при неизменном tileVersion. Details не требуют queryId; ответ проверяется по
  tileVersion/objectId/datasetVersion.
- `MapController` привязывает preload точек к активному выпуску выбранного
  формата. `MapDataClient` исключает cable_line из pinned запросов, проверяет
  dataset до batch, отменяет старые поколения и учитывает pin в контексте кэша.
- Новый source имеет native maxzoom=14, `promoteId=objectId`, displayMaxzoom=18.
  Кабели вставляются перед первым слоем точек, legacy cable source скрывается.
  Solid/dashed, min_zoom и точный поиск `,id,` в layerIds используют общий helper.
- Выбор фрагмента получает полную геометрию и рисует отдельный жёлтый overlay.
  В память object client кабели не добавляются. Переключение режима очищает выбор;
  поздние details не возвращают старую подсветку.
- Измерение использует координаты клика без привязки к линиям.
- Banner показывает отставание выпуска от active source snapshot. Новый или
  пустой live catalog после sync не отключает точки retained выпуска. Счётчики
  live catalog в tile режиме заменены на «—», чтобы не выдавать их за counts
  pinned dataset.

## Проверки

`npm --prefix frontend run test`: 41 passed. Typecheck и production build прошли
(предупреждение Vite о размере MapLibre chunk сохраняется).

`reports/tiles/frontend-vector/audit.json` — реальный Nuxt + FastAPI + disposable
PostGIS + headless Chrome. В fixture опубликован vector старого dataset с node100,
а active source переключён на новый пустой dataset. В браузере всё равно виден
node100: он пришёл через pin, вместе с lag banner. Native source14 overzoom на
карте15, фильтры1/11, порядок слоёв, полный cable_group:5, измерение без snap,
переход none без pin, отсутствие uncaught exceptions подтверждены.

Кадр: `reports/tiles/frontend-vector/vector-selected.png` (тестовая подложка пуста).
Отдельный HTTP smoke подтвердил 304 через proxy, запрет чужой карты/пути и
отсутствие server token в SSR HTML. Reports не содержат Bearer или подписанных URL.

Для воспроизведения `scripts/serve-tile-frontend-fixture.py` требует явный
`TEST_MAP_DB_URL` на localhost/127.0.0.1 и имя БД, заканчивающееся `_test`.
Он использует fixture из backend/tests, печёт/publish vector, создаёт новый
пустой source snapshot и обслуживает API на 127.0.0.1:8009. Временный token находится
только в chmod0600 `/tmp/map-tiles-frontend-private.json`. После остановки helper
удаляет свои token/snapshots/releases/files. Сам disposable DB container удаляется
после проверки.

Запустите Nuxt на 3019 с internal URL `http://127.0.0.1:8009`, server token из
private temp файла, `NUXT_PUBLIC_CABLE_TILES=vector`, координатами центра 37,55 и zoom15.
Отдельный Chrome profile с CDP 9225 нужен для
`node scripts/validate-tile-frontend-browser.mjs` (APP_URL/CDP_URL/OUTPUT_DIR
можно переопределить). Диагностический map handle включается только в dev и
только с query `mapDiagnostics=1`.

## Обновления и raster (этап 9)

Raster source: native maxzoom15, логический tileSize256, displayMaxzoom18.
DPR≥1.5 выбирает template2x, иначе template. Нет фильтров/hit-test/dash по кабелям;
UI поясняет, что тематический фильтр действует только на точки. Точки и details
используют datasetVersion выбранного формата, даже если форматы опубликованы
на разных datasets (в эксплуатации оба bake всё равно получают один dataset).

Один TileRefreshLoop на карту объединяет одновременные сигналы. Focus/visibility
работают независимо от poll. При poll0 остаётся таймер до templateExpiresAt;
transient failures повторяются с 1/2/4/.../30s backoff, auth ошибки останавливают
автоповторы, invalid signature не запускает loop. Повторные 410/expiry сигналы
одного вида ограничены 5s; ручной повтор остаётся доступен. Metadata запрос
ограничен 10s и отменяется при destroy. После destroy таймер не возобновляется.

Смена только подписи пересоздаёт source кабелей с новым template, сохраняя
point source, client dataset и полный selection overlay. Это исключает повтор
ранее загруженных XYZ по старому URL внутри tile cache MapLibre. Смена tileVersion
отменяет загрузки, очищает выбор и обновляет источник и pin точек. Source.errors
показывают код/status без полного URL с подписью. Нет автоматического перехода
в none при runtime ошибке. Ручной повтор также перезапрашивает failed XYZ.

`reports/tiles/frontend-raster-refresh/audit.json`: настоящий Nuxt/FastAPI/PostGIS
и Chrome при DPR1/2. Raster показывает node101 другого dataset; vector — node100.
256px/15/@2x, порядок под точками, отсутствие cable hit-test проверены. HMAC TTL10s,
poll0: source template обновился, preload count и выбранная трасса сохранились.
После заморозки вкладки на11s и focus подпись восстановилась без preload.
Nuxt dev sockets отключены в harness, чтобы HMR reconnect не пересоздавал документ;
production HMR не содержит. Для отдельной проверки retirement профилактический
таймер удержан: два настоящих publish удаляют старый выпуск; XYZ/preload получают
410, версия обновляется, выбор очищается и происходит ровно один catalog request.
В данном browser gate первым сигналом может быть pinned preload410, который
отменяет ещё незавершённые старые XYZ. Native callback burst проверен отдельно
тестом 100 signals → one flight. Uncaught exceptions:0 при обеих плотностях.

Снимки raster-dpr1.png/raster-dpr2.png в том же каталоге (пустая тестовая подложка).
Helper поддерживает TILE_FRONTEND_RASTER=1 (отдельный dataset/node101) и
TILE_FRONTEND_SIGNING=hmac с TILE_FRONTEND_SIGNATURE_TTL (default10s). Два publish
для retirement запускаются только через его local test endpoint
POST /fixture/advance-vector; production API этот endpoint не содержит.
Диагностические map/controller handles существуют только в dev с mapDiagnostics=1.
Temporary metadata token действует2days для длительной диагностики; plaintext
остаётся в private temp file, а helper удаляет его и DB digest после остановки.
Ни сигнатуры templates, ни Bearer не сохраняются в reports.

HTTP proxy regression дополнительно проверяет session cookie без сервисного
токена: proxy не добавляет пустой Authorization, который блокировал cookie
на backend. При server token клиентские credentials корректно заменяются.

Итоговая приёмка этапа 10, рабочая публикация и production browser gates:
[cable-tiles-validation.md](cable-tiles-validation.md).
