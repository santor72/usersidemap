CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS map_data;

CREATE TABLE map_data.sync_runs (
    id uuid PRIMARY KEY,
    source_system text NOT NULL,
    map_id bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'interrupted')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    rules_version text NOT NULL,
    source_snapshot text,
    stats jsonb NOT NULL DEFAULT '{}',
    error_code text
);

CREATE TABLE map_data.datasets (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES map_data.sync_runs(id),
    source_system text NOT NULL,
    map_id bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    config jsonb NOT NULL DEFAULT '{}',
    UNIQUE (source_system, map_id, id)
);

CREATE TABLE map_data.active_datasets (
    source_system text NOT NULL,
    map_id bigint NOT NULL,
    dataset_id uuid NOT NULL,
    PRIMARY KEY (source_system, map_id),
    FOREIGN KEY (source_system, map_id, dataset_id)
        REFERENCES map_data.datasets(source_system, map_id, id)
);

CREATE TABLE map_data.source_records (
    run_id uuid NOT NULL REFERENCES map_data.sync_runs(id) ON DELETE CASCADE,
    kind text NOT NULL,
    source_id bigint NOT NULL,
    parent_id bigint,
    position integer,
    data jsonb NOT NULL,
    PRIMARY KEY (run_id, kind, source_id)
);
CREATE INDEX source_records_parent_idx
    ON map_data.source_records(run_id, kind, parent_id, position, source_id);

CREATE TABLE map_data.features (
    dataset_id uuid NOT NULL REFERENCES map_data.datasets(id) ON DELETE CASCADE,
    source_kind text NOT NULL,
    source_id bigint NOT NULL,
    object_type text NOT NULL,
    title text NOT NULL DEFAULT '',
    geom geometry(Geometry, 4326) NOT NULL,
    min_zoom integer NOT NULL CHECK (min_zoom BETWEEN 0 AND 24),
    hidden boolean NOT NULL DEFAULT false,
    planned boolean NOT NULL DEFAULT false,
    length_m double precision,
    vertex_count integer NOT NULL CHECK (vertex_count >= 1),
    style jsonb NOT NULL DEFAULT '{}',
    properties jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (dataset_id, source_kind, source_id),
    CHECK (NOT ST_IsEmpty(geom)),
    CHECK (ST_IsValid(geom)),
    CHECK (ST_XMin(Box3D(geom)) >= -180 AND ST_XMax(Box3D(geom)) <= 180
       AND ST_YMin(Box3D(geom)) >= -90 AND ST_YMax(Box3D(geom)) <= 90),
    CHECK ((object_type NOT IN ('node','coupler') OR GeometryType(geom)='POINT')
       AND (object_type <> 'cable_line' OR GeometryType(geom)='LINESTRING'))
);
CREATE INDEX features_geom_gix ON map_data.features USING gist(geom);
CREATE INDEX features_visibility_idx ON map_data.features(dataset_id, object_type, min_zoom);

CREATE TABLE map_data.feature_links (
    dataset_id uuid NOT NULL,
    source_kind text NOT NULL,
    source_id bigint NOT NULL,
    role text NOT NULL,
    target_kind text NOT NULL,
    target_id bigint NOT NULL,
    PRIMARY KEY (dataset_id, source_kind, source_id, role),
    FOREIGN KEY (dataset_id, source_kind, source_id)
        REFERENCES map_data.features ON DELETE CASCADE
);

CREATE TABLE map_data.layers (
    dataset_id uuid NOT NULL REFERENCES map_data.datasets(id) ON DELETE CASCADE,
    source_id bigint NOT NULL,
    title text NOT NULL,
    default_visible boolean NOT NULL,
    style jsonb NOT NULL DEFAULT '{}',
    object_uuids jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (dataset_id, source_id)
);
CREATE TABLE map_data.feature_layers (
    dataset_id uuid NOT NULL,
    source_kind text NOT NULL,
    source_id bigint NOT NULL,
    layer_id bigint NOT NULL,
    PRIMARY KEY (dataset_id, source_kind, source_id, layer_id),
    FOREIGN KEY (dataset_id, source_kind, source_id)
        REFERENCES map_data.features ON DELETE CASCADE,
    FOREIGN KEY (dataset_id, layer_id) REFERENCES map_data.layers ON DELETE CASCADE
);
CREATE INDEX feature_layers_lookup_idx ON map_data.feature_layers(dataset_id, layer_id);

CREATE TABLE map_data.sync_issues (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES map_data.sync_runs(id) ON DELETE CASCADE,
    source_kind text NOT NULL,
    source_id bigint NOT NULL,
    severity text NOT NULL CHECK (severity IN ('warning','error')),
    code text NOT NULL,
    detail text NOT NULL
);
CREATE INDEX sync_issues_run_idx ON map_data.sync_issues(run_id, severity);

-- Preload leases protect an immutable dataset while its batches are in flight.
CREATE TABLE map_data.query_leases (
    id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES map_data.datasets(id),
    expires_at timestamptz NOT NULL,
    query jsonb NOT NULL,
    batches jsonb NOT NULL
);
CREATE INDEX query_leases_expiry_idx ON map_data.query_leases(expires_at);
