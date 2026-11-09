-- Appending the collection state preserves existing enum ordinals. MODIFY is
-- repeatable if DDL commits but recording the migration version is interrupted.
ALTER TABLE pending_object_uploads
    MODIFY COLUMN state ENUM('uploading','ready','collecting') NOT NULL;
