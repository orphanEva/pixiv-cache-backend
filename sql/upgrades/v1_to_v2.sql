-- MANUAL UPGRADE ONLY: pixiv_archive schema v1 -> v2
-- v1 is the seven-table archive schema used before schema_version tracking.
-- This script does not alter or delete archive data.
USE pixiv_archive;

CREATE TABLE IF NOT EXISTS schema_version (
  id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
  version INT UNSIGNED NOT NULL,
  applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT chk_schema_version_singleton CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO schema_version (id, version)
VALUES (1, 2)
ON DUPLICATE KEY UPDATE
  version = VALUES(version),
  applied_at = CURRENT_TIMESTAMP;
