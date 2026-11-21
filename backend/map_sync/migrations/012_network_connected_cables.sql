CREATE INDEX feature_links_cable_endpoints_idx
    ON map_data.feature_links (dataset_id, target_id, source_id)
    WHERE target_kind = 'node' AND source_kind = 'cable' AND role IN ('start', 'end');
