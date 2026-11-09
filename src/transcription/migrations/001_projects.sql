CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    google_sub VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
    email VARCHAR(320) NOT NULL,
    name VARCHAR(255) NOT NULL DEFAULT '',
    picture TEXT NOT NULL,
    created DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS projects (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    owner_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    title VARCHAR(200) NOT NULL,
    created DOUBLE NOT NULL,
    updated DOUBLE NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id),
    UNIQUE KEY project_owner (id, owner_id),
    KEY projects_owner (owner_id, updated)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS jobs (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    project_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    owner_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    filename VARCHAR(200) NOT NULL,
    status ENUM('queued','running','completed','failed','cancelled') NOT NULL,
    created DOUBLE NOT NULL,
    updated DOUBLE NOT NULL,
    queued_at DOUBLE NOT NULL,
    request JSON NOT NULL,
    progress JSON NOT NULL,
    result JSON,
    payloads JSON NOT NULL,
    error TEXT,
    claim_token CHAR(32),
    lease_until DOUBLE,
    attempt INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (owner_id) REFERENCES users(id),
    FOREIGN KEY (project_id, owner_id) REFERENCES projects(id, owner_id) ON DELETE CASCADE,
    KEY jobs_owner (owner_id, created),
    KEY jobs_queue (status, queued_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS owner_schedule (
    owner_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    last_served DOUBLE NOT NULL DEFAULT 0
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS worker_slot (
    id INTEGER PRIMARY KEY,
    job_id CHAR(32),
    token CHAR(32),
    lease_until DOUBLE NOT NULL DEFAULT 0,
    CHECK (id = 1)
) ENGINE=InnoDB;
INSERT IGNORE INTO worker_slot(id) VALUES (1);
CREATE TABLE IF NOT EXISTS artifacts (
    job_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    path VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    object_key VARCHAR(512) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    size BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (job_id, path),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS revisions (
    job_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    generation VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    result JSON NOT NULL,
    request JSON NOT NULL,
    payloads JSON NOT NULL,
    created DOUBLE NOT NULL,
    PRIMARY KEY (job_id, generation),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS object_garbage (
    object_key VARCHAR(512) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    created DOUBLE NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS legacy_imports (
    source CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    legacy_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    owner_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    job_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (source, legacy_id),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB;
