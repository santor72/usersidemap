CREATE TABLE map_data.api_tokens (
    id text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
    name text NOT NULL,
    map_ids bigint[] NOT NULL CHECK (cardinality(map_ids)>0 AND 0<ALL(map_ids)),
    scopes text[] NOT NULL CHECK (cardinality(scopes)>0 AND scopes <@ ARRAY['metadata','details']::text[]),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE FUNCTION map_data.api_pin_tile_dataset(requested_source text, requested_map bigint,
    requested_format text, requested_version uuid)
RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,map_data AS $$
    SELECT d.id FROM map_data.tile_versions v JOIN map_data.active_tile_versions a
        ON a.source_system=v.source_system AND a.map_id=v.map_id AND a.format=v.format
        AND v.id IN (a.active_version,a.prev_version)
    JOIN map_data.datasets d ON d.id=v.dataset_id
    WHERE v.id=requested_version AND v.source_system=requested_source
        AND v.map_id=requested_map AND v.format=requested_format
        AND v.status='ready' AND d.status='ready' FOR SHARE OF a,d
$$;
REVOKE ALL ON FUNCTION map_data.api_pin_tile_dataset(text,bigint,text,uuid) FROM PUBLIC;
