# Publish, rollback и восстановление файлов: этап 6

2026-10-08. `map_tiles.publication` завершает lifecycle для обоих форматов.
Публикация независима от bake: vector/raster команды оставляют validated staging,
а publish явно выбирает UUID. Все операции держат один session lease карты/формата.

## Команды

```sh
map-tiles --env .env publish --format vector --version <validatedUUID>
map-tiles --env .env publish --format raster --version <validatedUUID>
map-tiles --env .env rollback --format vector --version <prevUUID>
map-tiles --env .env cleanup --format vector
map-tiles --env .env cleanup --format raster
```

DSN выбирается по --format: MAP_TILES_VECTOR_DATABASE_URL либо
MAP_TILES_RASTER_DATABASE_URL, без fallback на owner/API/source. Source/map/root
берутся из общих worker settings. Роли worker изменяют только свой format;
DELETE строк не требуется — удалённый выпуск остаётся tombstone.

Rollback без --version меняет active/prev местами. Для безопасного повтора после
потери подтверждения commit используйте явный --version: если target уже active,
команда проверяет файлы и возвращает alreadyActive без второго переключения.
Удалённый/retired/чужой UUID невозможно вернуть указателем.

## Атомарная публикация и доступные выпуски

1. Locked recovery и cleanup своего формата. Выбранный UUID исключается из
   orphan cleanup для возможности повторить publish после rename-before-commit.
2. Проверка ready dataset pin, immutable DB manifest/identity и полного disk
   validate: coverage, checksums, decode, dimensions, никаких новых файлов.
3. Fsync files/directories; atomic rename staging→final на том же filesystem,
   fsync обоих родителей. Перенос не копирует и не перезаписывает каталог.
4. Под общим dataset transaction lock: новый ready+active, бывший active→prev,
   бывший prev→retired/pending. Deferred constraints проверяют ровно active+prev,
   не более двух ready в области source/map/format.
5. После commit invalidate Redis для всех затронутых UUID, затем GC retired.

Порядок session format lock → dataset transaction lock → pointer rows совпадает
с этапом 3. Длительная проверка файлов не держит общий dataset lock, snapshot
удерживается FK/live version. SQL snapshot cleanup не удаляет active/prev.
Опубликованные файлы и manifest не дописываются; rollback читает их и меняет
только указатели. Byte equality до/после rollback проверена для обоих форматов.

Сбой до rename оставляет validated staging и прежний active. Сбой после rename,
до успешного commit оставляет validated final и прежний active. Повтор publish
с тем же UUID проверяет final и завершает commit. Одновременно staging и final —
ошибка, файл не перезаписывается. Сбой после commit не откатывает публикацию:
повтор того же UUID возвращает alreadyActive, invalidate повторяется. Для ready
prev используется rollback, publish не переключает его неявно.

CLI JSON разделяет `published` и `cleanup.cleanupPending`. Успешная публикация
с failed GC возвращает published=true и code 2; это не означает отмены commit.
Обычный успех — code 0; ошибка операции — code 1; отмена bake — code 130.
Для анализа результата всегда учитывайте JSON и статус БД, а не только exit code.

## Повторяемая очистка

GC выбирает только зарегистрированные failed/retired версии этой карты/формата
со статусом pending/error. Active/prev дополнительно проверяются перед удалением.
Канонические paths проверяются на escape/symlink; чужие и неизвестные каталоги
не удаляются. rmtree не следует вложенным symlinks; замена корня выпуска на
symlink блокирует очистку и сохраняет внешние файлы.

Удаляются staging и final, затем fsync родителей, status=deleted/cleanup=done и
invalidate. Если процесс умер после удаления, до DB status update, повтор видит
отсутствующие каталоги и завершает tombstone. Ошибка FS сохраняет cleanup=error
и cleanup_error_code=FILE_CLEANUP_FAILED (миграция 009), не затирая исходную ошибку
bake. До успеха повторной очистки новый bake этого формата блокируется. Другой
формат продолжает bake/publish.

Общий cleanup также распознаёт validated final без ready как незавершённый rename
и переводит его в failed/UNPUBLISHED_FINAL для удаления. Для сохранения такого
выпуска повторите publish его UUID до запуска общего cleanup/нового bake.
Validated staging ожидает явной публикации и не удаляется общим GC.

Bake вызывает cleanup в начале и в конце, включая попытку после ошибки/отмены.
При нормальной очистке failed staging исчезает, deleted tombstone сохраняется;
при ошибке GC остаётся pending/error и повторяется на следующем запуске.
Rollback допускается при ошибке GC, если retained prev проходит полную проверку;
результат отдельно сообщает cleanupPending. Это не разрешает новую сборку.

## Доказательства

На disposable PostGIS + Redis и реальных файлах:

- три публикации vector и raster оставляют ровно два ready/final каталога;
  старый prev становится deleted, GC не меняет retained payload;
- rollback на другой datasetVersion и повтор с явным target; байты файлов
  до/после одинаковы;
- injections before/after rename и before/after commit, реальная ошибка
  deferred SQL constraint на commit; active не меняется до commit, retry работает;
- GC failure после успешного publish отдельно от publication success, rollback
  при pending GC, независимость raster при failed vector GC;
- сбой после удаления до записи tombstone, повтор cleanup, orphan final recovery;
- missing prev не меняет active; GC с symlink не удаляет внешние файлы;
- Redis invalidate на реальных publish/rollback/retire/delete для обоих форматов;
  операции под SET ROLE map_tiles_vector/map_tiles_raster (не owner).

Docker worker также выполнил настоящий CLI bake двух выпусков → publish →
повтор publish → targeted rollback → повтор rollback → cleanup. Отдельный
fixture map4, LOGIN map_tiles_vector и filesystem uid10001, без изменения
рабочей БД. Отчёт: `reports/tiles/publication-cli.json`.

120 backend regression tests passed, 0 skipped, 124.45s. После расширения
проверок на оба формата — 14 publication gates passed, 0 skipped, 68.99s.
Ruff и Docker worker build/CLI passed. Этап 6 принят.

Regression результаты и статус приёмки: `cable-tiles-validation.md`.
HTTP catalog datasetVersion после rollback, 410 и missing-file/cache race будут
проверены на этапе 7. Stage 6 не подменяет эти API gates SQL-проверками.
