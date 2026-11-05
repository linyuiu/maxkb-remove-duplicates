CREATE TABLE settings (id BIGINT PRIMARY KEY, config TEXT NOT NULL);

CREATE TABLE batches (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
                    config TEXT NOT NULL
                , queue_id TEXT, ordinal BIGINT NOT NULL DEFAULT 0, wave_state TEXT NOT NULL DEFAULT 'active');

CREATE TABLE events (
                    id BIGSERIAL PRIMARY KEY, batch_id TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL
                );

CREATE TABLE queues (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
                    batch_size BIGINT NOT NULL, auto_import BIGINT NOT NULL
                , source_key TEXT, intake_state TEXT NOT NULL DEFAULT 'receiving');

CREATE TABLE knowledge_preferences (
                    id BIGINT PRIMARY KEY, tags TEXT NOT NULL
                );

CREATE TABLE document_tag_receipts (
                    attempt_id TEXT NOT NULL, document_id TEXT NOT NULL,
                    tag_ids TEXT NOT NULL, applied_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY(attempt_id,document_id)
                );

CREATE TABLE source_inventory (
                    job_id TEXT NOT NULL, source_key TEXT NOT NULL, path TEXT NOT NULL,
                    bytes BIGINT NOT NULL, mtime TEXT NOT NULL, kind TEXT NOT NULL,
                    local_path TEXT NOT NULL, sha256 TEXT, state TEXT NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(job_id,path)
                );

CREATE TABLE retained_sources (
                file_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, metadata TEXT NOT NULL
            );

CREATE TABLE history_cleanups (
                id TEXT PRIMARY KEY, created_at DOUBLE PRECISION NOT NULL, state TEXT NOT NULL,
                payload TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
            );

CREATE TABLE ssh_sources (
                    id TEXT PRIMARY KEY, config TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL
                );

CREATE TABLE source_jobs (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, config TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL, total_files BIGINT NOT NULL DEFAULT 0,
                    done_files BIGINT NOT NULL DEFAULT 0, total_bytes BIGINT NOT NULL DEFAULT 0,
                    done_bytes BIGINT NOT NULL DEFAULT 0, skipped BIGINT NOT NULL DEFAULT 0,
                    current_path TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '等待执行',
                    cancel_requested BIGINT NOT NULL DEFAULT 0,
                    queue_id TEXT
                );

CREATE TABLE source_job_logs (
                    id BIGSERIAL PRIMARY KEY, job_id TEXT NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL, message TEXT NOT NULL
                );

CREATE TABLE source_remote_scans (
                source_key TEXT PRIMARY KEY, mirror_id TEXT NOT NULL,
                job_id TEXT NOT NULL, scanned_at DOUBLE PRECISION NOT NULL, result TEXT NOT NULL
            );

CREATE TABLE files (
                    ingest_order BIGSERIAL UNIQUE,
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    name TEXT NOT NULL, bytes BIGINT NOT NULL, status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
                    started_at DOUBLE PRECISION, job_id TEXT, job_status TEXT, source_id TEXT,
                    document_count BIGINT NOT NULL DEFAULT 0,
                    paragraph_count BIGINT NOT NULL DEFAULT 0, previewed_at DOUBLE PRECISION, prepared_at DOUBLE PRECISION
                , mineru_config TEXT, stop_requested BIGINT NOT NULL DEFAULT 0, operation_active BIGINT NOT NULL DEFAULT 0, finished_at DOUBLE PRECISION, split_started_at DOUBLE PRECISION, kind TEXT NOT NULL DEFAULT 'pdf', import_mode TEXT NOT NULL DEFAULT 'split', remote_paragraph_count BIGINT NOT NULL DEFAULT 0, relative_path TEXT NOT NULL DEFAULT '', queue_position BIGINT NOT NULL DEFAULT 0, attempt_id TEXT, import_attempts BIGINT NOT NULL DEFAULT 0, import_started_at DOUBLE PRECISION, import_finished_at DOUBLE PRECISION, next_check DOUBLE PRECISION NOT NULL DEFAULT 0, monitor_errors BIGINT NOT NULL DEFAULT 0, reimport_requested BIGINT NOT NULL DEFAULT 0, unresolved_create BIGINT NOT NULL DEFAULT 0, maxkb_config TEXT, parse_seconds DOUBLE PRECISION, parse_state TEXT NOT NULL DEFAULT 'pending', parse_error TEXT NOT NULL DEFAULT '', parse_started_at DOUBLE PRECISION, parse_finished_at DOUBLE PRECISION, split_seconds DOUBLE PRECISION, cleanup_target TEXT NOT NULL DEFAULT 'import_failed', import_seconds DOUBLE PRECISION);

CREATE TABLE knowledge_plans (
                    queue_id TEXT PRIMARY KEY REFERENCES queues(id) ON DELETE CASCADE,
                    plan TEXT NOT NULL
                );

CREATE TABLE knowledge_targets (
                    queue_id TEXT NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
                    folder TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
                    knowledge_id TEXT, tag_ids TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '', updated_at DOUBLE PRECISION NOT NULL, candidates TEXT NOT NULL DEFAULT '[]', reused BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY(queue_id,folder)
                );

CREATE TABLE knowledge_document_tags (
                    queue_id TEXT NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, tag_ids TEXT NOT NULL,
                    PRIMARY KEY(queue_id,path)
                );

CREATE TABLE paragraphs (
                    file_id TEXT NOT NULL REFERENCES files(id), position BIGINT NOT NULL,
                    document_name TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                    PRIMARY KEY(file_id, position)
                );

CREATE TABLE import_attempts (
                    id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id),
                    created_at DOUBLE PRECISION NOT NULL, finished_at DOUBLE PRECISION, phase TEXT NOT NULL,
                    payload TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
                , mode TEXT NOT NULL DEFAULT 'split');

CREATE TABLE audit_logs (
                    id BIGSERIAL PRIMARY KEY, file_id TEXT REFERENCES files(id),
                    created_at DOUBLE PRECISION NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL
                );

CREATE TABLE intake_receipts (
                    file_id TEXT PRIMARY KEY REFERENCES files(id),
                    source_kind TEXT NOT NULL, source_key TEXT NOT NULL,
                    source_job_id TEXT, source_path TEXT NOT NULL,
                    source_mtime TEXT, original_name TEXT NOT NULL, kind TEXT NOT NULL,
                    bytes BIGINT NOT NULL, sha256 TEXT NOT NULL, local_path TEXT NOT NULL,
                    received_at DOUBLE PRECISION NOT NULL
                , origin TEXT NOT NULL DEFAULT '{}');

CREATE TABLE source_archives (
                    file_id TEXT PRIMARY KEY REFERENCES files(id),
                    status TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL
                );

CREATE TABLE remote_resources (
                    file_id TEXT NOT NULL REFERENCES files(id), resource_id TEXT NOT NULL,
                    role TEXT NOT NULL, endpoint TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL, acquired_at DOUBLE PRECISION NOT NULL,
                    deleted_at DOUBLE PRECISION, error TEXT NOT NULL DEFAULT '', retained_at DOUBLE PRECISION, cleanup_via TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(file_id,resource_id)
                );

CREATE TABLE remote_calls (
                    id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id),
                    kind TEXT NOT NULL, state TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
                    finished_at DOUBLE PRECISION, response_path TEXT, error TEXT NOT NULL DEFAULT ''
                );

CREATE TABLE remote_documents (
                    attempt_id TEXT NOT NULL REFERENCES import_attempts(id),
                    document_id TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
                    source_file_id TEXT,
                    raw_status TEXT, error TEXT NOT NULL DEFAULT '',
                    checked_at DOUBLE PRECISION, deleted_at DOUBLE PRECISION, expected_paragraph_count BIGINT,
                    PRIMARY KEY(attempt_id,document_id)
                );

CREATE INDEX files_queue ON files(status, created_at);

CREATE INDEX files_batch ON files(batch_id, created_at);

CREATE INDEX files_history ON files(created_at DESC,id DESC);

CREATE INDEX events_batch ON events(batch_id, id);

CREATE INDEX files_parse_list ON files(batch_id,kind,parse_state);

CREATE INDEX batches_queue ON batches(queue_id, ordinal);

CREATE INDEX batches_history ON batches(created_at DESC,id DESC);

CREATE INDEX queues_history ON queues(created_at DESC,id DESC);

CREATE INDEX files_monitor ON files(status, next_check);

CREATE INDEX attempts_file ON import_attempts(file_id,created_at);

CREATE INDEX audit_file ON audit_logs(file_id,id);

CREATE INDEX audit_age ON audit_logs(created_at);

CREATE INDEX events_age ON events(created_at);

CREATE INDEX files_kind_queue ON files(kind,status,created_at);

CREATE INDEX knowledge_pending ON knowledge_targets(status);

CREATE INDEX receipts_source
                    ON intake_receipts(source_key,source_job_id);

CREATE INDEX resources_id ON remote_resources(endpoint,resource_id);

CREATE INDEX inventory_reuse ON source_inventory(local_path,bytes,mtime,updated_at DESC);

CREATE UNIQUE INDEX source_one_active
                    ON source_jobs(source_key) WHERE status IN ('queued','running');

CREATE INDEX source_job_order ON source_jobs(kind,status,created_at);

CREATE INDEX source_logs_job ON source_job_logs(job_id,id);
