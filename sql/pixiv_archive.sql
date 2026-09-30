-- MANUAL ONLY: pixiv_archive (MySQL 8.0+).
-- This replaces the old three-table pixiv_cache schema for NEW installations.
-- Review backups/migrations separately for existing installations.
-- No DROP TABLE statements. Run once on a NEW, EMPTY database.
CREATE DATABASE IF NOT EXISTS pixiv_archive CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE pixiv_archive;

CREATE TABLE series (
  id VARCHAR(32) NOT NULL PRIMARY KEY,
  type ENUM('novel','manga') NOT NULL,
  title VARCHAR(255) NOT NULL,
  caption TEXT,
  author_id VARCHAR(32) NOT NULL,
  author_name VARCHAR(128) NOT NULL,
  published_total INT UNSIGNED NOT NULL DEFAULT 0,
  is_completed TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_series_author (author_id),
  KEY idx_series_type (type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE tags (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(128) NOT NULL,
  translated_name VARCHAR(128),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_tag_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE works (
  id VARCHAR(32) NOT NULL PRIMARY KEY,
  type ENUM('illust','manga','ugoira','novel') NOT NULL,
  status ENUM('active','deleted','private','unavailable','auth_required','error') NOT NULL DEFAULT 'active',
  status_reason VARCHAR(1000),
  x_restrict TINYINT UNSIGNED NOT NULL DEFAULT 0,
  is_ai TINYINT(1) NOT NULL DEFAULT 0,
  series_id VARCHAR(32),
  series_order INT UNSIGNED,
  title VARCHAR(255) NOT NULL,
  caption TEXT,
  author_id VARCHAR(32) NOT NULL,
  author_name VARCHAR(128) NOT NULL,
  page_count INT UNSIGNED NOT NULL DEFAULT 1,
  remote_create_at DATETIME,
  remote_update_at DATETIME NULL,
  novel_content MEDIUMTEXT,
  raw_meta JSON,
  version_token CHAR(64) NOT NULL,
  current_version_no INT UNSIGNED NOT NULL DEFAULT 1,
  last_checked_at DATETIME,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_type_status (type,status),
  KEY idx_author_id (author_id),
  KEY idx_remote_update_at (remote_update_at),
  KEY idx_series_lookup (series_id, series_order),
  CONSTRAINT fk_works_series FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE work_history (
  history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  work_id VARCHAR(32) NOT NULL,
  version_num INT UNSIGNED NOT NULL,
  version_token CHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  caption TEXT,
  tags_snapshot JSON,
  remote_update_at DATETIME NULL,
  novel_content MEDIUMTEXT,
  raw_meta JSON NOT NULL,
  storage_path VARCHAR(1000) NOT NULL,
  archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_work_version (work_id, version_num),
  CONSTRAINT fk_wh_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE work_files (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  work_id VARCHAR(32) NOT NULL,
  page_index INT UNSIGNED NOT NULL DEFAULT 0,
  file_type ENUM('image','video','ugoira_zip','ugoira_meta','ugoira_mp4','novel_txt','cover') NOT NULL,
  file_path VARCHAR(1000) NOT NULL,
  file_size BIGINT UNSIGNED NOT NULL DEFAULT 0,
  file_hash CHAR(64),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_work_id (work_id),
  CONSTRAINT fk_wf_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE work_history_files (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  history_id BIGINT UNSIGNED NOT NULL,
  page_index INT UNSIGNED NOT NULL DEFAULT 0,
  file_type ENUM('image','video','ugoira_zip','ugoira_meta','ugoira_mp4','novel_txt','cover') NOT NULL,
  backup_path VARCHAR(1000) NOT NULL,
  file_size BIGINT UNSIGNED NOT NULL DEFAULT 0,
  file_hash CHAR(64),
  remote_url TEXT,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_history_id (history_id),
  CONSTRAINT fk_whf_history_id FOREIGN KEY (history_id) REFERENCES work_history(history_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE work_tag_relation (
  work_id VARCHAR(32) NOT NULL,
  tag_id INT UNSIGNED NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (work_id,tag_id),
  KEY idx_tag_id (tag_id),
  CONSTRAINT fk_wtr_tag_id FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE,
  CONSTRAINT fk_wtr_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
