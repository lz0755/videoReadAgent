CREATE INDEX ix_search_docs_scope_version
    ON search_docs(asset_id, analysis_id, index_version);
