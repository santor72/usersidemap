# Запуск карты в Docker

Стек включает Nuxt, FastAPI, PostGIS 16 / 3.5, автоматические миграции и
одноразовые команды импорта и очистки.
Локально и на сервере используется один `compose.yml` и корневой `.env`.
Нужен Docker Engine с Compose v2. Команды выполняются из корня проекта.
Python и uv на сервере не требуются.

## Первый запуск

```sh
cp -n .env.example .env
mkdir -p reports/sync
```

Если `.env` уже существует, сохраните его и добавьте недостающие параметры из
`.env.example`.

Заполните `MAP_DB_PASSWORD` и `MAP_API_DB_PASSWORD` разными паролями.
Задайте ключи SeaweedFS по `.env.example` и укажите UID/GID владельца каталога
отчётов в `SYNC_REPORT_UID` и `SYNC_REPORT_GID`.
Заполните `US_DB_*` для доступа к исходной Userside только на чтение.
Используйте адрес исходной БД, доступный из контейнера: `localhost` внутри
контейнера означает сам контейнер. На Docker Desktop хост доступен как
`host.docker.internal`. Значения паролей с `$` заключайте в одинарные кавычки
в env-файле. URL-encoding не нужен: внутренние DSN строятся автоматически.
Файл с настройками не копируется в Docker-образ.

```sh
docker compose up -d --build --wait
docker compose run --rm jobs sync
curl --fail http://127.0.0.1:8000/health/ready
curl --fail 'http://127.0.0.1:8000/api/map/layers?mapId=3'
```

На первом запуске `db` создает БД, `migrate` применяет миграции и выдает grants
роли `map_api`, после чего запускается `api`. `/health/ready` проверяет БД и
схему; до успешного импорта запрос слоев возвращает `503 DATASET_UNAVAILABLE`.
Импорт запускается вручную, чтобы запуск сервера не зависел от исходной Userside.
Обычный `docker compose up -d --build --wait` запускает одинаковый стек в обоих окружениях.

По умолчанию импорт публикует корректные объекты, а ошибки сохраняет в
`map_data.sync_issues` и `reports/sync/`. Для строгой проверки:

```sh
docker compose run --rm jobs sync --invalid-objects fail
```

В строгом режиме порог ошибок по умолчанию равен нулю. Предыдущая опубликованная
версия сохраняется при неудачном импорте. API использует отдельную роль PostGIS.

## Сеть и параметры

PostGIS доступна только внутри Compose-сети, порт БД на хост не публикуется.
API по умолчанию опубликован на `127.0.0.1:8000`: на сервере направьте на него
существующий reverse proxy с TLS и контролем доступа. Текущий API не содержит
авторизации. Для доступа в доверенной сети задайте `MAP_API_BIND` и
`MAP_API_PORT`; для фронтенда задайте точные `MAP_API_CORS_ORIGINS` через запятую.

Лимиты, таймауты и параметры пула `MAP_API_*` из корневого `.env.example`
можно добавить в `.env`. Внутри контейнера порт всегда 8000.
Источник выбирается через `US_SOURCE_SYSTEM`, карта импорта — `US_MAP_ID`.
Пароли административной роли и API задаются отдельно; роль `map_api` может
читать объекты и записывать только служебные таблицы загрузок.

Python-образ собирается с зависимостями из `backend/uv.lock`, без dev-пакетов.
Приложение работает под UID 10001, с файловой системой только для чтения.
PostGIS запускается как linux/amd64, на Apple Silicon используется эмуляция.
На сервере для релизов задавайте `MAP_IMAGE_TAG`, например `2026-10-04`.

## Обновление и обслуживание

```sh
docker compose build
docker compose stop api
docker compose run --rm --no-deps migrate
docker compose up -d --wait
```

Перед обновлением сделайте резервную копию. Повторный запуск миграций безопасен;
при их ошибке не запускайте новую версию API. Для обновления только данных:

```sh
docker compose run --rm jobs sync
docker compose run --rm jobs cleanup --keep 2
docker compose ps -a
docker compose logs --tail=100 api migrate
```

`sync` и `cleanup` можно запускать из планировщика сервера; выберите расписание
по частоте обновления Userside. Публикация данных происходит транзакционно,
активные загрузки удерживают свою версию. API продолжает работать во время импорта.

## Резервная копия и остановка

```sh
mkdir -p backups
docker compose exec -T db pg_dump -U map -d map -Fc > backups/map.dump
docker compose down
```

`down` сохраняет volume `userside-map_map_postgis`. `down -v` удаляет данные.
Новый пароль `MAP_DB_PASSWORD` в env-файле не меняет пароль уже созданной
PostgreSQL-роли: для ротации требуется `ALTER ROLE` в БД. Пароль API обновляется
при запуске `migrate`, затем пересоздайте `api`.

Для восстановления в **пустую** целевую БД остановите API, поднимите только `db`
и выполните:

```sh
docker compose up -d --wait db
docker compose exec -T db pg_restore -U map -d map --no-owner --no-privileges --exit-on-error < backups/map.dump
docker compose run --rm --no-deps migrate
docker compose up -d --wait
```

Дамп содержит данные карты и служебные загрузки; храните его с ограниченным доступом.
Compose автоматически читает `compose.yml` и `.env` в корне проекта.
Различия окружений задаются значениями `.env` (адрес источника, пароли, CORS
и адрес публикации API).

Порядок старта соответствует
[условиям depends_on в Compose](https://docs.docker.com/compose/how-tos/startup-order/),
сборка Python использует
[рекомендации uv для Docker](https://docs.astral.sh/uv/guides/integration/docker/).

Проверено 2026-10-04 на отдельном Compose-проекте: сборка образа, запуск на
пустой PostGIS, пять миграций, повторный запуск без новых миграций, readiness,
слои, preload, GeoJSON-батч и детали на синтетическом объекте, команда очистки
и сохранение данных после `down` / `up`. Проверены ограниченные права API,
пароль с символами `:@/$` и отсутствие исходных/административных реквизитов
в окружении API. Модульные проверки: 31 тест прошел, Ruff без ошибок.
Повторный импорт реальной Userside внутри этого стека не выполнялся.
