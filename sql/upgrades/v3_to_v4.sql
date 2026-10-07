-- MANUAL UPGRADE ONLY: pixiv_archive schema v3 -> v4
-- Adds work-to-sync-source attribution.
USE pixiv_archive;

CREATE TABLE work_sources (
  work_id VARCHAR(32) NOT NULL,
  source_id BIGINT UNSIGNED NOT NULL,
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (work_id, source_id),
  KEY idx_work_sources_source (source_id, last_seen_at),
  CONSTRAINT fk_ws_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
  CONSTRAINT fk_ws_source_id FOREIGN KEY (source_id) REFERENCES sync_sources(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

UPDATE schema_version
SET version = 4, applied_at = CURRENT_TIMESTAMP
WHERE id = 1 AND version = 3;
