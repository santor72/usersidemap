# Userside Map

Карта оптических кабелей, узлов и муфт из Userside. Синхронизатор читает исходную БД, публикует снимок в PostGIS, а FastAPI отдаёт данные приложению Nuxt с MapLibre.

## Первый запуск

Понадобятся Docker с Compose и доступ к базе Userside из контейнера. Выполните из корня репозитория:

```sh
cp -n .env.example .env
mkdir -p reports/sync
```

Заполните в `.env` подключение `US_DB_*` с правами чтения, пароли PostGIS и ключи SeaweedFS по шаблону. Для записи отчётов задайте `SYNC_REPORT_UID` и `SYNC_REPORT_GID` по выводу `id -u` и `id -g`.

```sh
docker compose up -d --build --wait
docker compose run --rm jobs sync
```

Откройте карту на [localhost:3000](http://localhost:3000/). Команда `jobs sync` обновляет данные для режима «Объекты (none)». Для импорта и публикации обоих форматов кабельных тайлов используйте:

```sh
docker compose --profile tools run --rm --build update-worker
```

Подробнее о [развёртывании](docs/docker-deploy.md), [синхронизации](backend/README.md) и [кабельных тайлах](docs/cable-tiles-operations.md).

## Структура проекта

- `backend/` — синхронизация, миграции PostGIS, FastAPI и сборка тайлов.
- `frontend/` — приложение Nuxt и модули работы с данными карты и MapLibre.
- `deploy/` и `scripts/` — запуск контейнеров, обслуживание и проверки.
