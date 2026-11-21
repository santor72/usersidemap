CREATE OR REPLACE FUNCTION map_data.check_mutable_dataset()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE version_id uuid;
DECLARE version_status text;
BEGIN
    version_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.dataset_id ELSE NEW.dataset_id END;
    -- Hold the status stable until this write commits; publication must wait.
    SELECT status INTO version_status FROM map_data.datasets
        WHERE id=version_id FOR SHARE;
    IF version_status = 'ready' THEN
        RAISE EXCEPTION 'Published datasets are immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.dataset_id <> NEW.dataset_id THEN
        SELECT status INTO version_status FROM map_data.datasets
            WHERE id=OLD.dataset_id FOR SHARE;
        IF version_status = 'ready' THEN
            RAISE EXCEPTION 'Published datasets are immutable';
        END IF;
    END IF;
    -- No parent row remains during cascading deletion of a dataset.
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
