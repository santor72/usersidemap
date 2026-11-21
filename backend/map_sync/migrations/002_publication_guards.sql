CREATE FUNCTION map_data.check_mutable_dataset() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE version_id uuid;
BEGIN
    version_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.dataset_id ELSE NEW.dataset_id END;
    -- Cascading deletion of an unreferenced dataset is allowed.
    IF EXISTS (SELECT 1 FROM map_data.datasets WHERE id=version_id AND status='ready') THEN
        RAISE EXCEPTION 'Published datasets are immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.dataset_id <> NEW.dataset_id AND EXISTS (
        SELECT 1 FROM map_data.datasets WHERE id=OLD.dataset_id AND status='ready'
    ) THEN
        RAISE EXCEPTION 'Published datasets are immutable';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

CREATE TRIGGER features_immutable BEFORE INSERT OR UPDATE OR DELETE ON map_data.features
    FOR EACH ROW EXECUTE FUNCTION map_data.check_mutable_dataset();
CREATE TRIGGER links_immutable BEFORE INSERT OR UPDATE OR DELETE ON map_data.feature_links
    FOR EACH ROW EXECUTE FUNCTION map_data.check_mutable_dataset();
CREATE TRIGGER layers_immutable BEFORE INSERT OR UPDATE OR DELETE ON map_data.layers
    FOR EACH ROW EXECUTE FUNCTION map_data.check_mutable_dataset();
CREATE TRIGGER membership_immutable BEFORE INSERT OR UPDATE OR DELETE ON map_data.feature_layers
    FOR EACH ROW EXECUTE FUNCTION map_data.check_mutable_dataset();

CREATE FUNCTION map_data.check_publication() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM map_data.datasets WHERE id=NEW.dataset_id AND status='ready') THEN
        RAISE EXCEPTION 'Only validated datasets can be active';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER active_ready BEFORE INSERT OR UPDATE ON map_data.active_datasets
    FOR EACH ROW EXECUTE FUNCTION map_data.check_publication();

CREATE FUNCTION map_data.check_dataset_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'building' THEN
        RAISE EXCEPTION 'Finished datasets are immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER dataset_transition BEFORE UPDATE ON map_data.datasets
    FOR EACH ROW EXECUTE FUNCTION map_data.check_dataset_transition();
