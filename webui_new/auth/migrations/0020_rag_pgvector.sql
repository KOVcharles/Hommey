CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_collections (
    collection_name TEXT PRIMARY KEY,
    active_version TEXT,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    index_fingerprint TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rag_index_versions (
    collection_name TEXT NOT NULL REFERENCES rag_collections(collection_name) ON DELETE CASCADE,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'active', 'retired', 'failed')),
    index_fingerprint TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    schema_version TEXT NOT NULL,
    build_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    PRIMARY KEY (collection_name, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_index_versions_one_active
    ON rag_index_versions(collection_name)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS rag_documents (
    collection_name TEXT NOT NULL,
    index_version TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version TEXT NOT NULL,
    source_path TEXT,
    source_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection_name, index_version, document_id, document_version),
    FOREIGN KEY (collection_name, index_version)
        REFERENCES rag_index_versions(collection_name, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    collection_name TEXT NOT NULL,
    index_version TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version TEXT NOT NULL,
    chunk_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL,
    embedding VECTOR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection_name, index_version, chunk_id),
    UNIQUE (collection_name, index_version, document_id, document_version, chunk_hash),
    FOREIGN KEY (collection_name, index_version)
        REFERENCES rag_index_versions(collection_name, version) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_rag_chunks_active_lookup
    ON rag_chunks(collection_name, index_version);

CREATE INDEX IF NOT EXISTS ix_rag_chunks_document
    ON rag_chunks(collection_name, index_version, document_id);

ALTER TABLE rag_collections
    DROP CONSTRAINT IF EXISTS fk_rag_collections_active_version;

ALTER TABLE rag_collections
    ADD CONSTRAINT fk_rag_collections_active_version
    FOREIGN KEY (collection_name, active_version)
    REFERENCES rag_index_versions(collection_name, version)
    DEFERRABLE INITIALLY DEFERRED;
