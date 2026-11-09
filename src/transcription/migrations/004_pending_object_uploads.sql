-- No job FK: rejected or interrupted submissions still need object cleanup.
CREATE TABLE IF NOT EXISTS pending_object_uploads (
    object_key VARCHAR(512) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    state ENUM('uploading','ready') NOT NULL,
    expires DOUBLE NOT NULL,
    KEY pending_upload_expiry (expires)
) ENGINE=InnoDB;
