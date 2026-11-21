-- API manifests are relational so workers can share query state and cleanup can
-- cascade through a lease without storing an unbounded ID array in JSONB.
ALTER TABLE map_data.query_leases
    ALTER COLUMN batches SET DEFAULT '[]'::jsonb,
    ADD CONSTRAINT query_leases_id_dataset_uq UNIQUE (id, dataset_id);
COMMENT ON COLUMN map_data.query_leases.batches IS
    'Compact batch summary only: [{index, objectCount, vertexCount, oversized}]. Object keys are in query_items.';

CREATE TABLE map_data.query_batches (
    query_id uuid NOT NULL REFERENCES map_data.query_leases(id) ON DELETE CASCADE,
    batch_index integer NOT NULL CHECK (batch_index >= 0),
    object_count integer NOT NULL CHECK (object_count >= 0),
    vertex_count bigint NOT NULL CHECK (vertex_count >= 0),
    oversized boolean NOT NULL DEFAULT false,
    PRIMARY KEY (query_id, batch_index)
);
CREATE TABLE map_data.query_items (
    query_id uuid NOT NULL,
    batch_index integer NOT NULL,
    item_order integer NOT NULL CHECK (item_order >= 0),
    dataset_id uuid NOT NULL,
    source_kind text NOT NULL,
    source_id bigint NOT NULL,
    object_type text NOT NULL,
    vertex_count integer NOT NULL CHECK (vertex_count > 0),
    FOREIGN KEY (query_id, dataset_id)
      REFERENCES map_data.query_leases(id, dataset_id) ON DELETE CASCADE,
    PRIMARY KEY (query_id, source_kind, source_id),
    UNIQUE (query_id, batch_index, item_order),
    FOREIGN KEY (query_id, batch_index)
      REFERENCES map_data.query_batches(query_id, batch_index) ON DELETE CASCADE,
    FOREIGN KEY (dataset_id, source_kind, source_id)
      REFERENCES map_data.features(dataset_id, source_kind, source_id) ON DELETE CASCADE
);
CREATE INDEX query_items_batch_idx ON map_data.query_items(query_id, batch_index, item_order);

CREATE TABLE map_data.query_lease_totals (
    query_id uuid PRIMARY KEY REFERENCES map_data.query_leases(id) ON DELETE CASCADE,
    total_objects integer NOT NULL CHECK (total_objects >= 0),
    total_vertices bigint NOT NULL CHECK (total_vertices >= 0),
    type_counts jsonb NOT NULL DEFAULT '{}'::jsonb
);
