# Эксплуатация кабельных тайлов и подключение GIS

Сервис читает отдельную PostGIS БД со snapshot. Sync публикует dataset, но не
собирает тайлы. Vector и raster публикуются независимо, каждый хранит active и
prev. При отставании карта показывает banner; периодический опрос catalog по
умолчанию выключен (`TILES_LAG=banner`, `TILES_CATALOG_POLL_SECONDS=0`).

## Автоматический полный цикл

Полный цикл выполняет контейнер `update-worker`. На хосте нужен Docker с
Compose; Python и библиотеки обработки геометрии устанавливаются в образ.
После изменения кода один раз соберите worker:

```sh
docker compose --profile tools build update-worker
```

Для каждого обновления выполните одну команду из каталога проекта:

```sh
docker compose --profile tools run --rm update-worker
```

Compose ждёт готовности БД, SeaweedFS и применения миграций. Worker последовательно выполняет:

1. `map-db sync` — импорт свежего snapshot из Userside.
2. Vector bake с `datasetId` именно этого успешного sync.
3. Raster bake с тем же `datasetId`, включая 1x/@2x.
4. Publish vector, затем publish raster.
5. Cleanup объектов обоих форматов.

UUID извлекаются автоматически. Оба bake должны успешно пройти до начала
публикации. Форматы публикуются отдельными транзакциями: если второй publish
не прошёл, первый уже может быть активным. Повторный запуск продолжает со
второго publish. API поддерживает разные datasetVersion форматов.

По умолчанию worker использует `SYNC_INVALID_OBJECTS=skip`: некорректные
объекты исключаются из snapshot, а все корректные данные публикуются и
используются для новых тайлов. Каждый исключённый объект сохраняется в
`sync_issues` и JSON/Markdown отчёте; количество выводится в лог и checkpoint
(`excludedObjects`). Число исключений не ограничивается фиксированным порогом.
Пустой snapshot, ошибка подключения к БД или ошибка всего импорта по-прежнему
останавливают цикл.

Чтобы включить строгую проверку, задайте `SYNC_INVALID_OBJECTS=fail` в `.env`
или передайте `--invalid-objects fail` после `update-worker`. В этом режиме
`--max-errors` задаёт допуск ошибок (по умолчанию 0). Незавершённый sync при
повторном запуске использует текущую политику; уже успешный sync не повторяется.
При ручном `jobs sync` политика также берётся из `SYNC_INVALID_OBJECTS`.
Вызов `map-db sync` вне Compose без этой переменной остаётся строгим.

Worker не использует Docker socket и не запускает соседние контейнеры.
Он получает доступ к источнику и destination owner для sync; subprocess
сборки получает только DSN роли своего формата, без owner/source credentials.
API и отдельные `tiles-vector`/`tiles-raster` сохраняют прежние права.

Журнал и отчёты хранятся в постоянном volume `map_updates`, внутри контейнера
`/updates/<cycleUUID>.json`, checkpoint `/updates/latest.json`, отчёты sync
`/updates/sync/`. Удаление контейнера через `--rm` не удаляет журнал.
Старые журналы хостового скрипта в `reports/update/` не переносятся автоматически.
Посмотреть состояние или отчёты можно без запуска обновления:

```sh
docker compose --profile tools run --rm --no-deps --entrypoint cat update-worker /updates/latest.json
docker compose --profile tools run --rm --no-deps --entrypoint ls update-worker /updates/sync
```

После обычной ошибки повторите ту же команду: успешные шаги не повторяются,
dataset не заменяется новым sync. После completed следующий вызов начинает
свежий sync. Exit2 publish означает состоявшуюся публикацию с pending GC:
worker завершает второй publish и проверяет cleanup без повторной публикации.
Прерывание завершает subprocess; при следующем вызове незавершённый шаг
выполняется снова. Сохранённые результаты CLI с подтверждённым exit code
восстанавливаются из приватных файлов в volume, если checkpoint не успел
записаться. После аварийного завершения без exit receipt шаг повторяется;
при таком сбое sync может создать дополнительный snapshot. Bake использует
существующий механизм восстановления прерванных сборок.

Блокировка `/updates/update.lock` запрещает пересечение запусков с этим volume;
второй получает exit75. Остальные коды: 0 — готово, 1 — ошибка,
2 — cleanup остаётся незавершённым, 130 — прерывание. ОС снимает блокировку
после завершения процесса. Используйте один scheduler для карты; DB locks
также защищают sync и сборку каждого формата. Не удаляйте `map_updates`,
пока цикл не завершён. Retention snapshot datasets через `jobs cleanup`
и retention отчётов/cron.log настраиваются отдельно.

Пример cron для этого Mac (время запуска выберите самостоятельно):

```cron
PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin
# Пример: каждый час, не установлен автоматически.
0 * * * * cd /Users/santor/Documents/work/userside/map && docker compose --profile tools run --rm -T update-worker >> /Users/santor/Documents/work/userside/map/reports/update-worker-cron.log 2>&1
```

Перед добавлением cron один раз запустите команду вручную. Расписание запускайте
от владельца проекта, имеющего доступ к `.env` и Docker. Docker Desktop на Mac
должен работать; на сервере нужен Docker daemon. Worker расписание не меняет.
Хостовой `scripts/update-map.py` остаётся совместимым вариантом запуска,
но для cron рекомендуется контейнер.

## Compose и ручной выпуск

Локальная карта доступна с `http://localhost:3000` и `http://127.0.0.1:3000`:
оба origin входят в стандартный `MAP_API_CORS_ORIGINS`. При открытии карты по
другому адресу задайте его origin явно, иначе браузер блокирует прямые запросы
тайлов к API.

В `.env` задайте разные `MAP_DB_PASSWORD`, `MAP_API_DB_PASSWORD`,
`MAP_TILES_VECTOR_DB_PASSWORD`, `MAP_TILES_RASTER_DB_PASSWORD`. Файл содержит
секреты: права 0600, не добавлять в репозиторий. `migrate` создаёт роли;
отсутствующий пароль worker оставляет его роль NOLOGIN. API не получает пароль
владельца или Userside. Workers получают пароль только своей роли и ограничены
RLS своего формата. В API нет Cairo/WebP encoder.

```sh
docker compose build api tiles-vector frontend
docker compose up -d db redis api frontend
```

В Compose тайлы хранятся через S3 в SeaweedFS 4.48 (образ закреплён digest).
Данные лежат в `./tiles/seaweedfs`, приватный файл ключей — в
`./tiles/seaweedfs-config/s3.json`. Это bind-каталоги, не named volume тайлов.
SeaweedFS объединяет объекты в volume-файлы; отдельного файла на хосте для
каждого XYZ нет. S3 и служебные порты не публикуются на хост.
`SEAWEED_VOLUME_SIZE_MB=128` и `SEAWEED_VOLUME_MAX=32` задают около 4 GiB
ёмкости data volumes; при росте данных увеличьте лимит количества volumes.
Metadata и временное место vacuum учитываются дополнительно.
API и workers не монтируют данные SeaweedFS и обращаются только к S3.

Задайте в `.env` `TILES_STORAGE=s3`, `TILES_S3_BUCKET=map-tiles` и две разные пары
`TILES_S3_WORKER_ACCESS_KEY`/`TILES_S3_WORKER_SECRET_KEY`,
`TILES_S3_API_ACCESS_KEY`/`TILES_S3_API_SECRET_KEY`. Secrets должны иметь минимум
32 символа. `storage-init` генерирует файл прав 0600: worker читает, пишет,
перечисляет и удаляет объекты только своего bucket; API только читает.
Обновите SeaweedFS после смены ключей, чтобы перечитать конфигурацию.

При первом развёртывании создайте каталоги, доступные UID 10001:

```sh
mkdir -p tiles/seaweedfs tiles/seaweedfs-config
sudo chown -R 10001:10001 tiles/seaweedfs tiles/seaweedfs-config
chmod 700 tiles/seaweedfs tiles/seaweedfs-config
```

Worker отправляет PBF/WebP прямо в S3, без временного дерева XYZ. Ключи имеют
вид `3/raster/<tileVersion>/19/<x>/<y>.webp` (для vector — `.pbf`).
`.staging` — логическое состояние выпуска; объекты сразу лежат под окончательным
UUID. Publish проверяет полный состав, размеры и контрольные суммы, меняет
маркер `_release.json`, затем транзакционно переключает active/prev в PostgreSQL.
Копирования всех объектов при publish нет. Сбой между маркером и DB commit
восстанавливается теми же командами publish/cleanup. API разрешает только ready
выпуски и проверяет SHA256 каждого выдаваемого тайла; URL и подписи не меняются.

Каждый новый тайл независимо декодируется до загрузки. PUT передаёт ContentMD5
и проверяет возвращённый ETag; итоговый LIST сверяется с MD5 и размером каждого
объекта в immutable manifest. Это позволяет проверять большой выпуск без
повторного скачивания всех изображений. Старые manifests без MD5 проверяются
полным скачиванием и декодированием. Cleanup удаляет только failed/retired UUID,
не active/prev; удаление из S3 не означает немедленное уменьшение файлов volumes:
пространство возвращает vacuum SeaweedFS.

Перед переключением существующей файловой установки в S3 перенесите **оба**
формата (API пока должен оставаться в старом режиме):

```sh
docker compose up -d seaweedfs
docker compose run --rm --no-deps -v "$PWD/tiles:/legacy-tiles:ro" tiles-vector migrate-storage --format vector --source-root /legacy-tiles
docker compose run --rm --no-deps -v "$PWD/tiles:/legacy-tiles:ro" tiles-raster migrate-storage --format raster --source-root /legacy-tiles
docker compose up -d api
```

Миграция сохраняет UUID и DB manifests, переносит active/prev, скачивает обратно
**каждый** объект и сверяет SHA256. До проверки API сохраните исходные каталоги.
Для резервного копирования нужны PostgreSQL и `tiles/seaweedfs` вместе с
`tiles/seaweedfs-config` (согласованный backup при остановленном SeaweedFS).
Без S3 Python CLI по-прежнему поддерживает `TILES_STORAGE=filesystem` и `TILES_ROOT`.

`map_updates` с журналом полного цикла остаётся отдельным named volume.
Контейнеры имеют read-only root filesystem. Workers запускаются отдельными CLI
в профиле tools (вручную или orchestration script), с 2 CPU, 1 GiB памяти и
concurrency 2. Redis не публикует порт,
хранит только status cache с TTL 30s, без persistence; отказ Redis оставляет
чтение статуса из PostgreSQL. `/health/live` проверяет процесс, `/health/ready`
проверяет PostGIS и обязательную схему/функции, включая tile pin и tokens.
Отсутствие первого выпуска допустимо: catalog возвращает active=null, карта
при запуске выбирает none. Пропажа опубликованного файла даёт XYZ 503.

После sync возьмите UUID опубликованного snapshot из отчёта. **Один и тот же
явный UUID** передаётся обоим CLI; не вычисляйте «latest» отдельно для каждого.

```sh
docker compose run --rm jobs sync
docker compose run --rm tiles-vector vector --dataset <DATASET_UUID>
# Из последнего JSON CLI взять tileVersion:
docker compose run --rm tiles-vector publish --format vector --version <VECTOR_UUID>
docker compose run --rm tiles-raster raster --dataset <DATASET_UUID>
docker compose run --rm tiles-raster publish --format raster --version <RASTER_UUID>
```

Bake создаёт staging и валидирует весь native диапазон. В файловом режиме
publish делает fsync и атомарный rename, в S3 — переключает маркер выпуска;
затем фиксирует pointers в БД. Выпуски неизменяемы.
Новый выпуск вытесняет старый prev в retired и запускает GC. Ошибка cleanup
оставляет запись pending/error и блокирует следующий bake этого формата;
повторите `cleanup --format vector|raster` соответствующим worker.
Exit 2 publish означает, что публикация состоялась, но cleanup ещё нужен.
Не удаляйте каталоги или строки БД вручную.

```sh
docker compose run --rm tiles-vector rollback --format vector
# Или вернуть конкретный retained выпуск:
docker compose run --rm tiles-raster rollback --format raster --version <RETAINED_UUID>
docker compose run --rm tiles-raster cleanup --format raster
```

Rollback меняет active/prev, но не active dataset sync. Catalog возвращает
`datasetVersion` выбранного выпуска. Точки preload и details должны быть
закреплены за ним, даже если он старше текущего snapshot. Retained dataset
защищён от snapshot cleanup. UUID вне active/prev после retirement даёт 410.

Полный эксплуатационный замер, включая оба bake/validation/publish/GC:
`backend/.venv/bin/python scripts/validate-tile-compose-cycle.py --dataset <UUID>`.
Этот проверочный скрипт **публикует оба выпуска**, не запускает sync. Результат
`reports/tiles/deployment-cycle.json` содержит длительности и рекомендованный
grace (не менее 210s, 1.5× измеренный цикл, округление до 30s). Замер повторять
при росте карты/изменении CPU, памяти, диска или native zoom.

## Метрики и оповещения

`/metrics` предназначен для внутреннего scrape. Не публикуйте его на внешнем
reverse proxy. Он читает durable состояние БД: lag в секундах с публикации
нового active snapshot, retained count, pending cleanup, статус последнего
build. Cache hits/misses/errors — counters процесса API. Никаких токенов,
signed URLs, DSN или UUID выпуска в labels нет.

`TILES_LAG_GRACE_SECONDS=210` задаёт порог и должен покрывать измеренный цикл.
`deploy/tile-alerts.yml` проверяет превышение lag/grace без дополнительного
`for`. Failed/interrupted последний build даёт critical сразу при следующем
scrape/evaluation (по умолчанию до 30s), без grace. Новый retry снимает этот
алерт; pending cleanup и Redis outage имеют `for: 1m`. Lag снимается только
после публикации нужного dataset, а не после успешного bake staging.

```sh
docker compose --profile monitoring up -d prometheus
# http://localhost:9090/alerts (порт только loopback)
docker run --rm --entrypoint /bin/promtool -v "$PWD/deploy:/etc/prometheus:ro" \
  -w /etc/prometheus prom/prometheus:v3.5.0 test rules tile-alerts.test.yml
```

Compose включает только локальный Prometheus. Для доставки оповещений
подключите свой Alertmanager; получатели и отправка сообщений здесь не настроены.
Семантика правил: [официальная документация Prometheus](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/).

## Доступ GIS и токены

Admin CLI работает только с destination owner DSN. Plaintext token возвращается
один раз; БД хранит digest. Не сохраняйте stdout create в общие логи.

```sh
docker compose run --rm token-admin tokens create --name gis-client \
  --map 3 --scope metadata --scope details --ttl-seconds 2592000
docker compose run --rm token-admin tokens list
docker compose run --rm token-admin tokens revoke --id <TOKEN_ID>
```

Metadata требует `Authorization: Bearer <token>` со scope metadata, details —
scope details; map IDs также ограничены. Для Nuxt задайте приватный
`NUXT_MAP_TILES_TOKEN` (никогда `NUXT_PUBLIC_*`). Серверный proxy добавляет
Bearer, секрет не попадает в браузер. Без service token proxy может передать
авторизацию пользователя. Изменение `CABLE_TILES=none|vector|raster` требует
перезапуска frontend; runtime override — `NUXT_PUBLIC_CABLE_TILES`.
В включённом vector/raster deployment доступны все три режима. Raster не
поддерживает hit-test, cable theme filter и dash; theme фильтрует точки.

Запросите catalog:

```http
GET /api/maps/3/tiles/catalog
Authorization: Bearer <token>
```

`formats.vector` и `formats.raster` содержат active/prev или null. Descriptor:
`tileVersion`, `datasetVersion`, `minzoom`, `maxzoom`, `displayMaxzoom`,
`tileSize`, `template`, `template2x`, `templateExpiresAt`, `tilejsonUrl`,
`styleUrl`, `tilesBehindActive`, `capabilities`. Форматы могут ссылаться на
разные dataset. Используйте descriptor текущего режима.

```text
/api/maps/3/tiles/vector/<tileVersion>/tilejson.json
/api/maps/3/tiles/raster/<tileVersion>/tilejson.json
/api/maps/3/tiles/vector/<tileVersion>/style.json
/api/maps/3/tiles/<tileVersion>/objects/<sourceKind>/<sourceId>
```

TileJSON/style/catalog/details возвращают JSON с `ETag`,
`Cache-Control: private, no-cache`, `Vary: Authorization, Cookie`.
`If-None-Match` может дать 304, но доступ проверяется каждый раз. Details
возвращает полную snapshot geometry в WGS84 и связи, не clipped MVT и не
queryId. Стабильный vector `objectId` объединяет части одного кабеля;
не делайте отдельные details-запросы для каждого фрагмента.

XYZ берите из template, сохраняя query целиком:

```text
/tiles/maps/3/vector/<tileVersion>/{z}/{x}/{y}.pbf
/tiles/maps/3/raster/<tileVersion>/{z}/{x}/{y}.webp
/tiles/maps/3/raster/<tileVersion>/{z}/{x}/{y}@2x.webp
```

MVT: `application/vnd.mapbox-vector-tile`, `Content-Encoding: gzip`;
объекты в хранилище raw PBF, gzip выполняется API. Обычный HTTP клиент распакует
gzip сам; не распаковывайте повторно. Raster: `image/webp`, lossless RGBA,
256×256 для 1x и 512×512 для @2x. Обе плотности имеют одинаковую географию.
Нет объектов — валидный пустой MVT/прозрачный WebP, status 200.
Ошибки и 410 имеют `Cache-Control: no-store`; malformed/native out-of-range
XYZ — 422, ожидаемый файл отсутствует при ready — 503.

В Compose native vector maxzoom=14, raster maxzoom/prebakeMaxzoom=19;
displayMaxzoom=18. `TILES_RASTER_PREBAKE_MAX=19` покрывает логический zoom18.
Source maxzoom задайте из descriptor: сервер выдаёт заранее собранные тайлы,
не вычисляет новые по запросу. Vector tileSize512, raster tileSize256:
для raster XYZ z соответствует логическому zoom z−1. При текущем native19
в пределах zoom карты до18 нет увеличения растрового изображения; линии
на высоких zoom сохраняют штатную толщину. Рисование кабелей
начинается с min_zoom; short cable включён в native14/vector, native15/raster.
Ширины в обоих режимах узнаваемы, но не обязаны совпадать по пикселям.
Если снизить `TILES_RASTER_PREBAKE_MAX`, raster overzoom масштабирует готовое изображение: линии утолщаются и становятся
мягче при увеличении выше native; vector сохраняет ширину style в экранных px.

Vector source-layer — `cable_line`; порядок под всеми point layers. Векторный
style уже содержит color/width/dash и порядок backbone. `layerIds` хранит
строку вида `,1,11,`: проверяйте вхождение `,1,`, чтобы слой 1 не совпал с 11.
При пользовательском style учитывайте `min_zoom` и `renderOrder`.

## XYZ модели A/B, CDN и истечение

A (`TILES_XYZ_SIGNING=none`) выдаёт XYZ по известному UUID без Bearer. Случайный
UUID не является авторизацией. Ответ immutable, max-age до года. Отзыв
metadata token не отзывает уже известные XYZ или сохранённые клиентом тайлы;
retirement не очищает внешние кэши. Для закрытой карты нужен внешний периметр
для frontend, legacy `/api/map`, metadata и `/tiles`.

B (`hmac`) требует `TILES_HMAC_SECRET` не короче 32 символов. Template содержит
`exp` и `sig`; HMAC привязан к map/format/version path-prefix, подходит для
всех XYZ данного выпуска. Подпись проверяется **до 304**. Browser max-age
ограничен остатком TTL; `s-maxage=0, must-revalidate` требует origin revalidation
при CDN cache-hit. Не убирайте exp/sig из cache key, не включайте stale serving
или общий cache-hit без повторной проверки expiry/signature. Альтернатива —
собственный edge verifier с той же проверкой HMAC. Сохранённые клиентом данные
не отзываются. Смена secret инвалидирует новые origin запросы с прежней подписью.

Контроль Nginx cache выполнен с `proxy_cache_valid 200 1h`: origin headers
требуют повторной проверки, истёкший URL с If-None-Match даёт 403. Не включайте
`proxy_ignore_headers Cache-Control`: обработку заголовков описывает
[Nginx proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).
Воспроизведение локального gate (нужны disposable TEST_MAP_DB_URL и Redis):
`PYTHONPATH=backend/tests backend/.venv/bin/python -m pytest scripts/test_tile_proxy_gate.py -q`.

Обновляйте descriptor **до** `templateExpiresAt`, даже при poll=0. Наш frontend
делает refresh на 80% оставшегося TTL и по focus/visibility; обновляет только
кабельный source при неизменном tileVersion. GIS должен делать аналогично.
Истёкшая подпись — 403 `TILE_SIGNATURE_EXPIRED`, неверная — `TILE_SIGNATURE_INVALID`;
первую обновляйте, для второй проверьте конфигурацию. 410 `TILE_VERSION_RETIRED`
означает повторно получить catalog, сменить source и pin точек на dataset нового
выпуска. Ограничьте concurrent refresh и backoff, не делайте запрос catalog
на каждую ошибку сотен XYZ.

## Reverse proxy

Пример Nginx для закрытого deployment. TLS сертификат подготовьте в своём
окружении. Bearer metadata передаётся как Authorization. Для браузерной карты
подойдёт VPN/SSO access gateway перед всем hostname. Ниже периметр показан
через allow/deny подсеть VPN; замените адреса на свои.

```nginx
limit_req_zone $binary_remote_addr zone=map_tiles:10m rate=50r/s;

server {
    listen 443 ssl;
    server_name map.example.org;
    ssl_certificate /etc/nginx/tls/fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/privkey.pem;
    autoindex off;
    allow 10.20.0.0/16;   # пример VPN, обязательно заменить
    deny all;

    location = /metrics { return 404; }
    location /health/ { return 404; }
    location /tiles/ {
        limit_req zone=map_tiles burst=100 nodelay;
        proxy_pass http://api:8000;  # API проверяет version и HMAC до отдачи
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache off;            # безопасный начальный вариант для A/B
        access_log off;             # не записывать signed query URLs
    }
    location /api/maps/ {
        proxy_pass http://api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache off;
    }
    location / { proxy_pass http://frontend:3000; }
}
```

Никогда не добавляйте `alias`/`root` на TILES_ROOT, autoindex или StaticFiles:
это обходит retained/HMAC/status checks и раскрывает UUID/staging/manifest.
У API access log отключён; на proxy также исключите Authorization, Cookie,
exp/sig из логов. TLS termination доверяет только вашему proxy. Поставьте
`TILES_PUBLIC_BASE_URL=https://map.example.org`, задайте
`MAP_API_CORS_ORIGINS` точными GIS origins (через запятую). CORS разрешает
Authorization/If-None-Match и раскрывает ETag/Content-Encoding, но не заменяет
периметр. TLS/proxy/CDN здесь описаны для внешнего развёртывания; локальный
Compose по умолчанию публикует frontend/API только на loopback.
