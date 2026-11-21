CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE map_data.api_tokens
    DROP CONSTRAINT api_tokens_scopes_check,
    ADD CONSTRAINT api_tokens_scopes_check CHECK (
        cardinality(scopes) > 0 AND scopes <@ ARRAY['metadata','details','network:nearby']::text[]
    ),
    ADD CONSTRAINT api_tokens_network_map_check CHECK (
        NOT ('network:nearby' = ANY(scopes)) OR cardinality(map_ids) = 1
    );

CREATE INDEX features_nearby_geog_gix ON map_data.features
    USING gist (dataset_id, object_type, (geom::geography))
    WHERE NOT hidden AND NOT planned;
