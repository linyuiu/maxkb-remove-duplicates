
CREATE TABLE data_sources (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(150) NOT NULL, 
	connection TEXT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE users (
	id VARCHAR(36) NOT NULL, 
	username VARCHAR(100) NOT NULL, 
	password_hash TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (username)
)

;

CREATE TABLE scan_tasks (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(150) NOT NULL, 
	source_id VARCHAR(36) NOT NULL, 
	knowledge_ids JSON NOT NULL, 
	threshold FLOAT NOT NULL, 
	exclude_same_document BOOLEAN NOT NULL, 
	batch_size INTEGER NOT NULL, 
	cron VARCHAR(100), 
	timezone VARCHAR(80) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	next_fire TIMESTAMP WITH TIME ZONE, 
	skipped_fires INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_id) REFERENCES data_sources (id)
)

;

CREATE TABLE sessions (
	token_hash VARCHAR(64) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;

CREATE TABLE scan_runs (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	config JSON NOT NULL, 
	state VARCHAR(30) NOT NULL, 
	phase VARCHAR(30) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	token VARCHAR(36), 
	cancel_requested BOOLEAN NOT NULL, 
	snapshot_ready BOOLEAN NOT NULL, 
	cursor INTEGER NOT NULL, 
	total INTEGER NOT NULL, 
	processed INTEGER NOT NULL, 
	pair_count INTEGER NOT NULL, 
	error TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES scan_tasks (id)
)

;

CREATE TABLE paragraph_snapshots (
	id SERIAL NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	paragraph_id VARCHAR(36) NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (run_id, paragraph_id), 
	FOREIGN KEY(run_id) REFERENCES scan_runs (id)
)

;

CREATE TABLE similarity_pairs (
	id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	left_id VARCHAR(36) NOT NULL, 
	right_id VARCHAR(36) NOT NULL, 
	similarity FLOAT NOT NULL, 
	left_embedding_id VARCHAR(128) NOT NULL, 
	right_embedding_id VARCHAR(128) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (run_id, left_id, right_id), 
	FOREIGN KEY(run_id) REFERENCES scan_runs (id)
)

;

CREATE TABLE review_decisions (
	id VARCHAR(36) NOT NULL, 
	pair_id VARCHAR(36) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	note TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(pair_id) REFERENCES similarity_pairs (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;

CREATE TABLE source_operations (
	id VARCHAR(36) NOT NULL, 
	pair_id VARCHAR(36) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	target JSON NOT NULL, 
	kind VARCHAR(30) NOT NULL, 
	before JSON, 
	proposed JSON, 
	actual JSON, 
	state VARCHAR(30) NOT NULL, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(pair_id) REFERENCES similarity_pairs (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;
CREATE UNIQUE INDEX one_active_run_per_task ON scan_runs (task_id) WHERE state IN ('queued','running');
CREATE INDEX ix_scan_runs_state ON scan_runs (state);
CREATE INDEX ix_paragraph_snapshots_run_id ON paragraph_snapshots (run_id);
CREATE INDEX ix_similarity_pairs_run_id ON similarity_pairs (run_id);
CREATE INDEX pairs_run_score ON similarity_pairs (run_id, similarity, id);
