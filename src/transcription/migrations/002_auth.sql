CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    google_sub VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
    email VARCHAR(320) NOT NULL,
    name VARCHAR(200) NOT NULL DEFAULT '',
    picture VARCHAR(2048) NOT NULL DEFAULT '',
    created DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sessions (
    token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    expires DATETIME(6) NOT NULL,
    CONSTRAINT sessions_user_fk FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX sessions_expires_idx (expires),
    INDEX sessions_user_idx (user_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    browser_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    verifier VARCHAR(128) CHARACTER SET ascii NOT NULL,
    nonce VARCHAR(128) CHARACTER SET ascii NOT NULL,
    expires DATETIME(6) NOT NULL,
    INDEX oauth_states_expires_idx (expires)
) ENGINE=InnoDB;
