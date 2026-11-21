CREATE TABLE map_data.tile_builds (
    id uuid PRIMARY KEY,
    version_id uuid NOT NULL UNIQUE,
    source_system text NOT NULL,
    map_id bigint NOT NULL CHECK (map_id>0),
    format text NOT NULL CHECK (format IN ('vector','raster')),
    dataset_version uuid NOT NULL,
    dataset_id uuid,
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','interrupted')),
    stage text NOT NULL DEFAULT 'starting',
    parameters jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(parameters)='object'),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    error_code text,
    UNIQUE (id,source_system,map_id,format,dataset_version),
    CHECK (dataset_id IS NULL OR dataset_id=dataset_version),
    CHECK (status<>'running' OR dataset_id IS NOT NULL),
    CHECK ((status='running')=(finished_at IS NULL)),
    FOREIGN KEY (source_system,map_id,dataset_id)
        REFERENCES map_data.datasets(source_system,map_id,id) ON DELETE SET NULL (dataset_id)
);
CREATE UNIQUE INDEX tile_build_exclusive_idx ON map_data.tile_builds(map_id,format)
    WHERE status='running';
CREATE INDEX tile_build_dataset_idx ON map_data.tile_builds(dataset_id) WHERE status='running';

CREATE TABLE map_data.tile_versions (
    id uuid PRIMARY KEY,
    build_id uuid NOT NULL UNIQUE,
    source_system text NOT NULL,
    map_id bigint NOT NULL CHECK (map_id>0),
    format text NOT NULL CHECK (format IN ('vector','raster')),
    dataset_version uuid NOT NULL,
    dataset_id uuid,
    status text NOT NULL CHECK (status IN ('building','validated','ready','failed','retired','deleted')),
    staging_path text NOT NULL,
    final_path text NOT NULL,
    minzoom integer NOT NULL CHECK (minzoom BETWEEN 0 AND 24),
    maxzoom integer NOT NULL CHECK (maxzoom BETWEEN 14 AND 24),
    display_maxzoom integer NOT NULL CHECK (display_maxzoom BETWEEN 14 AND 24),
    rules_version text NOT NULL,
    rules_hash text NOT NULL,
    geometry_profile jsonb NOT NULL CHECK (jsonb_typeof(geometry_profile)='object'),
    tolerance_hash text NOT NULL,
    manifest jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(manifest)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    validated_at timestamptz,
    published_at timestamptz,
    retired_at timestamptz,
    deleted_at timestamptz,
    cleanup_status text NOT NULL DEFAULT 'none' CHECK (cleanup_status IN ('none','pending','done','error')),
    error_code text,
    UNIQUE (source_system,map_id,format,id),
    UNIQUE (build_id,id),
    CHECK (minzoom<=maxzoom AND maxzoom<=display_maxzoom+CASE WHEN format='raster' THEN 1 ELSE 0 END),
    CHECK (staging_path=map_id::text||'/'||format||'/.staging/'||id::text),
    CHECK (final_path=map_id::text||'/'||format||'/'||id::text),
    CHECK (dataset_id IS NULL OR dataset_id=dataset_version),
    CHECK (status NOT IN ('building','validated','ready') OR dataset_id IS NOT NULL),
    CHECK ((status IN ('building','validated','ready'))=(cleanup_status='none')),
    CHECK (status<>'deleted' OR cleanup_status='done'),
    FOREIGN KEY (source_system,map_id,dataset_id)
        REFERENCES map_data.datasets(source_system,map_id,id) ON DELETE SET NULL (dataset_id),
    FOREIGN KEY (build_id,source_system,map_id,format,dataset_version)
        REFERENCES map_data.tile_builds(id,source_system,map_id,format,dataset_version)
);
ALTER TABLE map_data.tile_builds ADD CONSTRAINT tile_build_version_fk
    FOREIGN KEY (id,version_id) REFERENCES map_data.tile_versions(build_id,id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX tile_version_dataset_idx ON map_data.tile_versions(dataset_id)
    WHERE status IN ('building','validated','ready');
CREATE INDEX tile_version_cleanup_idx ON map_data.tile_versions(map_id,format)
    WHERE cleanup_status IN ('pending','error');

CREATE TABLE map_data.active_tile_versions (
    source_system text NOT NULL,
    map_id bigint NOT NULL,
    format text NOT NULL CHECK (format IN ('vector','raster')),
    active_version uuid NOT NULL,
    prev_version uuid,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source_system,map_id,format),
    CHECK (active_version IS DISTINCT FROM prev_version),
    FOREIGN KEY (source_system,map_id,format,active_version)
        REFERENCES map_data.tile_versions(source_system,map_id,format,id),
    FOREIGN KEY (source_system,map_id,format,prev_version)
        REFERENCES map_data.tile_versions(source_system,map_id,format,id)
);

CREATE FUNCTION map_data.check_tile_pointer_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.source_system,NEW.map_id,NEW.format) IS DISTINCT FROM
       ROW(OLD.source_system,OLD.map_id,OLD.format) THEN
        RAISE EXCEPTION 'Tile pointer scope is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tile_pointer_identity BEFORE UPDATE ON map_data.active_tile_versions
    FOR EACH ROW EXECUTE FUNCTION map_data.check_tile_pointer_identity();

CREATE FUNCTION map_data.check_tile_version() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,map_data AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'building' OR NOT EXISTS (
            SELECT 1 FROM map_data.datasets d WHERE d.id=NEW.dataset_id AND d.status='ready'
        ) THEN RAISE EXCEPTION 'Tile release requires an explicit ready dataset'; END IF;
        RETURN NEW;
    END IF;
    IF ROW(NEW.id,NEW.build_id,NEW.source_system,NEW.map_id,NEW.format,NEW.dataset_version,
           NEW.staging_path,NEW.final_path,NEW.minzoom,NEW.maxzoom,NEW.display_maxzoom,
           NEW.rules_version,NEW.rules_hash,NEW.geometry_profile,NEW.tolerance_hash,NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id,OLD.build_id,OLD.source_system,OLD.map_id,OLD.format,OLD.dataset_version,
           OLD.staging_path,OLD.final_path,OLD.minzoom,OLD.maxzoom,OLD.display_maxzoom,
           OLD.rules_version,OLD.rules_hash,OLD.geometry_profile,OLD.tolerance_hash,OLD.created_at)
    THEN RAISE EXCEPTION 'Tile release identity and rules are immutable'; END IF;
    IF NEW.dataset_id IS DISTINCT FROM OLD.dataset_id AND NOT (
        OLD.status IN ('failed','retired','deleted') AND NEW.dataset_id IS NULL
    ) THEN RAISE EXCEPTION 'Tile release dataset is immutable'; END IF;
    IF OLD.status<>'building' AND NEW.manifest IS DISTINCT FROM OLD.manifest THEN
        RAISE EXCEPTION 'Validated tile manifest is immutable'; END IF;
    IF OLD.status IN ('ready','retired','deleted') AND
       ROW(NEW.validated_at,NEW.published_at) IS DISTINCT FROM ROW(OLD.validated_at,OLD.published_at)
    THEN RAISE EXCEPTION 'Published tile timestamps are immutable'; END IF;
    IF NEW.status<>OLD.status AND NOT (
        (OLD.status='building' AND NEW.status IN ('validated','failed')) OR
        (OLD.status='validated' AND NEW.status IN ('ready','failed')) OR
        (OLD.status='ready' AND NEW.status='retired') OR
        (OLD.status IN ('failed','retired') AND NEW.status='deleted')
    ) THEN RAISE EXCEPTION 'Invalid tile release transition'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tile_version_guard BEFORE INSERT OR UPDATE ON map_data.tile_versions
    FOR EACH ROW EXECUTE FUNCTION map_data.check_tile_version();

CREATE FUNCTION map_data.check_tile_build() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,map_data AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'running' THEN RAISE EXCEPTION 'Build must start running'; END IF;
        RETURN NEW;
    END IF;
    IF ROW(NEW.id,NEW.version_id,NEW.source_system,NEW.map_id,NEW.format,NEW.dataset_version,
           NEW.parameters,NEW.started_at) IS DISTINCT FROM
       ROW(OLD.id,OLD.version_id,OLD.source_system,OLD.map_id,OLD.format,OLD.dataset_version,
           OLD.parameters,OLD.started_at)
    THEN RAISE EXCEPTION 'Tile build identity and parameters are immutable'; END IF;
    IF NEW.dataset_id IS DISTINCT FROM OLD.dataset_id AND NOT (
        OLD.status<>'running' AND NEW.dataset_id IS NULL
    ) THEN RAISE EXCEPTION 'Tile build dataset is immutable'; END IF;
    IF OLD.status<>'running' AND
       ROW(NEW.status,NEW.stage,NEW.heartbeat_at,NEW.finished_at,NEW.error_code) IS DISTINCT FROM
       ROW(OLD.status,OLD.stage,OLD.heartbeat_at,OLD.finished_at,OLD.error_code)
    THEN RAISE EXCEPTION 'Finished tile build is immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tile_build_guard BEFORE INSERT OR UPDATE ON map_data.tile_builds
    FOR EACH ROW EXECUTE FUNCTION map_data.check_tile_build();

CREATE FUNCTION map_data.check_retained_tiles() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog,map_data AS $$
DECLARE scope_source text; scope_map bigint; scope_format text;
BEGIN
    scope_source:=CASE WHEN TG_OP='DELETE' THEN OLD.source_system ELSE NEW.source_system END;
    scope_map:=CASE WHEN TG_OP='DELETE' THEN OLD.map_id ELSE NEW.map_id END;
    scope_format:=CASE WHEN TG_OP='DELETE' THEN OLD.format ELSE NEW.format END;
    IF (SELECT count(*) FROM map_data.tile_versions WHERE source_system=scope_source
        AND map_id=scope_map AND format=scope_format AND status='ready')>2 OR EXISTS (
        SELECT 1 FROM map_data.tile_versions v WHERE v.source_system=scope_source
        AND v.map_id=scope_map AND v.format=scope_format AND v.status='ready'
        AND NOT EXISTS (SELECT 1 FROM map_data.active_tile_versions a
            WHERE a.source_system=scope_source AND a.map_id=scope_map AND a.format=scope_format
            AND v.id IN (a.active_version,a.prev_version))
    ) OR EXISTS (
        SELECT 1 FROM map_data.active_tile_versions a JOIN map_data.tile_versions v
            ON v.id IN (a.active_version,a.prev_version)
        WHERE a.source_system=scope_source AND a.map_id=scope_map AND a.format=scope_format
            AND v.status<>'ready'
    ) THEN RAISE EXCEPTION 'Ready releases must be exactly active and prev (at most two)'; END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER tile_retention_guard AFTER INSERT OR UPDATE OR DELETE
    ON map_data.tile_versions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION map_data.check_retained_tiles();
CREATE CONSTRAINT TRIGGER tile_pointer_guard AFTER INSERT OR UPDATE OR DELETE
    ON map_data.active_tile_versions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION map_data.check_retained_tiles();

CREATE FUNCTION map_data.protect_tile_dataset() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,map_data AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM map_data.tile_versions WHERE dataset_id=OLD.id
        AND status IN ('building','validated','ready')) OR EXISTS (
        SELECT 1 FROM map_data.tile_builds WHERE dataset_id=OLD.id AND status='running'
    ) THEN RAISE EXCEPTION 'Dataset retained by tile release or build'; END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER tile_dataset_guard BEFORE DELETE ON map_data.datasets
    FOR EACH ROW EXECUTE FUNCTION map_data.protect_tile_dataset();

CREATE FUNCTION map_data.tiles_pin_dataset(requested_source text, requested_map bigint,
                                          requested_dataset uuid)
RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,map_data AS $$
    SELECT id FROM map_data.datasets WHERE id=requested_dataset AND source_system=requested_source
        AND map_id=requested_map AND status='ready' FOR SHARE
$$;
REVOKE ALL ON FUNCTION map_data.tiles_pin_dataset(text,bigint,uuid) FROM PUBLIC;

-- Roles are provisioned by deploy/entrypoint.py. Policies can exist before roles.
-- API SELECT remains unrestricted; only explicit worker roles can INSERT/UPDATE.
ALTER TABLE map_data.tile_builds ENABLE ROW LEVEL SECURITY;
ALTER TABLE map_data.tile_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE map_data.active_tile_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tile_build_read ON map_data.tile_builds FOR SELECT USING (true);
CREATE POLICY tile_version_read ON map_data.tile_versions FOR SELECT USING (true);
CREATE POLICY tile_pointer_read ON map_data.active_tile_versions FOR SELECT USING (true);
CREATE POLICY tile_build_insert ON map_data.tile_builds FOR INSERT WITH CHECK (
    current_user='map_tiles_'||format);
CREATE POLICY tile_build_update ON map_data.tile_builds FOR UPDATE USING (
    current_user='map_tiles_'||format) WITH CHECK (current_user='map_tiles_'||format);
CREATE POLICY tile_version_insert ON map_data.tile_versions FOR INSERT WITH CHECK (
    current_user='map_tiles_'||format);
CREATE POLICY tile_version_update ON map_data.tile_versions FOR UPDATE USING (
    current_user='map_tiles_'||format) WITH CHECK (current_user='map_tiles_'||format);
CREATE POLICY tile_pointer_insert ON map_data.active_tile_versions FOR INSERT WITH CHECK (
    current_user='map_tiles_'||format);
CREATE POLICY tile_pointer_update ON map_data.active_tile_versions FOR UPDATE USING (
    current_user='map_tiles_'||format) WITH CHECK (current_user='map_tiles_'||format);
