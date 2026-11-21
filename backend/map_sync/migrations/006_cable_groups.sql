-- Existing datasets stay immutable; the new geometry type is used by the next sync.
DO $$
DECLARE constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid='map_data.features'::regclass AND contype='c'
          AND lower(pg_get_constraintdef(oid)) LIKE '%geometrytype%'
    LOOP
        EXECUTE format('ALTER TABLE map_data.features DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

ALTER TABLE map_data.features ADD CONSTRAINT features_geometry_type_check
CHECK ((object_type NOT IN ('node','coupler') OR GeometryType(geom)='POINT')
   AND (object_type <> 'cable_line' OR GeometryType(geom) IN ('LINESTRING','MULTILINESTRING')));
