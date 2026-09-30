-- Pixiv Cache Backend / MySQL 8.0+
-- Mirrors Alembic revision 0001_initial.
-- Choose ONE initialization method:
--   A. Preferred: let "alembic upgrade head" create these tables;
--   B. Manually run this SQL in a new database, then run "alembic stamp 0001_initial".
-- NEVER apply this DDL and then run "alembic upgrade head" on the same schema without stamping.

CREATE DATABASE IF NOT EXISTS pixiv_cache
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE pixiv_cache;

CREATE TABLE IF NOT EXISTS pixiv_work (
    id BIGINT NOT NULL AUTO_INCREMENT,
    pixiv_id BIGINT NOT NULL,
    work_type ENUM('ILLUST','NOVEL') NOT NULL,
    status ENUM('ACTIVE','UNAVAILABLE','DELETED','RESTRICTED','AUTH_REQUIRED','ERROR') NOT NULL,
    status_reason VARCHAR(1000) NULL,
    title VARCHAR(500) NOT NULL,
    author_id BIGINT NULL,
    author_name VARCHAR(255) NULL,
    remote_created_at DATETIME NULL,
    remote_updated_at DATETIME NULL,
    version_token VARCHAR(64) NOT NULL,
    current_version_no INT NOT NULL,
    cached_at DATETIME NOT NULL,
    last_checked_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_pixiv_work_type (pixiv_id, work_type),
    KEY ix_pixiv_work_pixiv_id (pixiv_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS pixiv_version (
    id BIGINT NOT NULL AUTO_INCREMENT,
    work_id BIGINT NOT NULL,
    version_no INT NOT NULL,
    version_token VARCHAR(64) NOT NULL,
    remote_updated_at DATETIME NULL,
    storage_path VARCHAR(1000) NOT NULL,
    metadata_json JSON NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_work_version (work_id, version_no),
    KEY ix_pixiv_version_work_id (work_id),
    CONSTRAINT fk_pixiv_version_work
        FOREIGN KEY (work_id) REFERENCES pixiv_work(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS pixiv_asset (
    id BIGINT NOT NULL AUTO_INCREMENT,
    version_id BIGINT NOT NULL,
    page_index INT NOT NULL,
    remote_url TEXT NULL,
    local_path VARCHAR(1000) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    PRIMARY KEY (id),
    KEY ix_pixiv_asset_version_id (version_id),
    CONSTRAINT fk_pixiv_asset_version
        FOREIGN KEY (version_id) REFERENCES pixiv_version(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
