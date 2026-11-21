-- Retain object identity/text after source staging and old datasets are removed.
ALTER TABLE map_data.sync_issues
    ADD COLUMN object_kind text,
    ADD COLUMN object_id bigint,
    ADD COLUMN object_title text NOT NULL DEFAULT '',
    ADD COLUMN object_description text NOT NULL DEFAULT '';
UPDATE map_data.sync_issues
SET object_kind=source_kind,object_id=source_id;
