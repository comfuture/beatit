-- Independent of jobs: deleting an owner job must not orphan paid remote work.
CREATE TABLE IF NOT EXISTS remote_calls (
    call_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    job_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    claim_token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created DOUBLE NOT NULL,
    KEY remote_claim (claim_token)
) ENGINE=InnoDB;
