-- Existing sessions keep their token and current expiry. Their absolute lifetime
-- starts at migration because the old schema did not record a creation time.
-- Column guards make this safe after MySQL's implicitly committed partial DDL.
SET @beatit_session_ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='sessions' AND column_name='created'),
    'SELECT 1',
    'ALTER TABLE beatit.sessions ADD COLUMN created DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)'
);
PREPARE beatit_session_statement FROM @beatit_session_ddl;
EXECUTE beatit_session_statement;
DEALLOCATE PREPARE beatit_session_statement;
SET @beatit_session_ddl = IF(
    EXISTS(SELECT 1 FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='sessions' AND column_name='absolute_expires'),
    'SELECT 1',
    'ALTER TABLE beatit.sessions ADD COLUMN absolute_expires DATETIME(6) NULL DEFAULT NULL'
);
PREPARE beatit_session_statement FROM @beatit_session_ddl;
EXECUTE beatit_session_statement;
DEALLOCATE PREPARE beatit_session_statement;
UPDATE beatit.sessions SET absolute_expires=DATE_ADD(created, INTERVAL 30 DAY)
WHERE absolute_expires IS NULL;
-- Nullable absolute_expires preserves old-image INSERT compatibility on rollback.
-- New code also applies the created + 30 day cap to such later legacy inserts.
