-- SELECT ... FOR SHARE needs UPDATE rights in PostgreSQL. Keep those rights
-- with the migration owner and expose only this narrowly scoped pin operation.
CREATE FUNCTION map_data.api_pin_active_dataset(requested_source text, requested_map bigint)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, map_data
AS $$
    SELECT a.dataset_id
    FROM map_data.active_datasets AS a
    JOIN map_data.datasets AS d ON d.id = a.dataset_id
    WHERE a.source_system = requested_source
      AND a.map_id = requested_map
      AND d.status = 'ready'
    FOR SHARE OF a
$$;

REVOKE ALL ON FUNCTION map_data.api_pin_active_dataset(text, bigint) FROM PUBLIC;
