-- MANUAL UPGRADE ONLY: pixiv_archive schema v2 -> v3
-- Adds persistent author/bookmark synchronization sources.
USE pixiv_archive;

CREATE TABLE sync_sources (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  type ENUM('author','bookmarks') NOT NULL,
  remote_user_id VARCHAR(32) NOT NULL,
  restrict_mode ENUM('public','private') NOT NULL DEFAULT 'public',
  include_illust TINYINT(1) NOT NULL DEFAULT 1,
  include_novel TINYINT(1) NOT NULL DEFAULT 1,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  interval_seconds INT UNSIGNED NOT NULL DEFAULT 21600,
  frontier_json JSON,
  next_run_at DATETIME,
  last_run_at DATETIME,
  last_success_at DATETIME,
  last_error VARCHAR(1000),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_sync_source_identity (type, remote_user_id, restrict_mode),
  KEY idx_sync_due (enabled, next_run_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

UPDATE schema_version
SET version = 3, applied_at = CURRENT_TIMESTAMP
WHERE id = 1 AND version = 2;
