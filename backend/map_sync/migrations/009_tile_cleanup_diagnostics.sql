-- File GC failure is separate from the original bake/publish diagnostic.
ALTER TABLE map_data.tile_versions ADD COLUMN cleanup_error_code text;
