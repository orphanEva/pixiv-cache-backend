-- MANUAL ONLY: pixiv_archive (MySQL 8.0+).
-- 仅用于新库初始化；运行时程序不会自动执行 DDL。
-- 已有数据库请按 sql/upgrades/ 中的手工升级脚本逐版本升级。
-- 本文件不包含 DROP TABLE，适用于全新的空数据库。
CREATE DATABASE IF NOT EXISTS pixiv_archive CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE pixiv_archive;

CREATE TABLE schema_version (
  id TINYINT UNSIGNED NOT NULL COMMENT '固定主键，仅允许为 1',
  version INT UNSIGNED NOT NULL COMMENT '当前数据库结构版本号',
  applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '结构版本最后确认或更新时间',
  PRIMARY KEY (id),
  CONSTRAINT chk_schema_version_singleton CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='数据库结构版本元数据';

INSERT INTO schema_version (id, version) VALUES (1, 4);

CREATE TABLE series (
  id VARCHAR(32) NOT NULL COMMENT 'Pixiv 系列 ID',
  type ENUM('novel','manga') NOT NULL COMMENT '系列类型：小说或漫画',
  title VARCHAR(255) NOT NULL COMMENT '系列标题',
  caption TEXT COMMENT '系列简介',
  author_id VARCHAR(32) NOT NULL COMMENT 'Pixiv 作者 ID',
  author_name VARCHAR(128) NOT NULL COMMENT '作者名称快照',
  published_total INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Pixiv 已发布作品总数',
  is_completed TINYINT(1) NOT NULL DEFAULT 0 COMMENT '系列是否已完结：0 否，1 是',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '本地首次创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '本地最后更新时间',
  PRIMARY KEY (id),
  KEY idx_series_author (author_id),
  KEY idx_series_type (type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Pixiv 小说或漫画系列信息';

CREATE TABLE tags (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '标签本地主键',
  name VARCHAR(128) NOT NULL COMMENT 'Pixiv 原始标签名称',
  translated_name VARCHAR(128) COMMENT 'Pixiv 提供的标签翻译名称',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '标签首次入库时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_tag_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='作品标签字典';

CREATE TABLE works (
  id VARCHAR(32) NOT NULL COMMENT 'Pixiv 作品 ID',
  type ENUM('illust','manga','ugoira','novel') NOT NULL COMMENT '作品类型：插画、漫画、动图或小说',
  status ENUM('active','deleted','private','unavailable','auth_required','error') NOT NULL DEFAULT 'active' COMMENT '当前远端可访问状态',
  status_reason VARCHAR(1000) COMMENT '状态异常或不可访问原因',
  x_restrict TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Pixiv 内容限制等级',
  is_ai TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否标记为 AI 生成作品：0 否，1 是',
  series_id VARCHAR(32) COMMENT '所属 Pixiv 系列 ID',
  series_order INT UNSIGNED COMMENT '作品在系列中的顺序',
  title VARCHAR(255) NOT NULL COMMENT '当前作品标题',
  caption TEXT COMMENT '当前作品简介或说明',
  author_id VARCHAR(32) NOT NULL COMMENT 'Pixiv 作者 ID',
  author_name VARCHAR(128) NOT NULL COMMENT '当前作者名称快照',
  page_count INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '作品页数或素材页数',
  remote_create_at DATETIME COMMENT 'Pixiv 作品创建时间（UTC 约定）',
  remote_update_at DATETIME NULL COMMENT 'Pixiv 作品最后更新时间（UTC 约定，可为空）',
  novel_content MEDIUMTEXT COMMENT '小说当前版本正文，仅小说使用',
  raw_meta JSON COMMENT 'Pixiv 当前原始元数据 JSON',
  version_token CHAR(64) NOT NULL COMMENT '当前版本内容指纹 SHA-256',
  current_version_no INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '当前生效的本地归档版本号',
  last_checked_at DATETIME COMMENT '最后一次成功检查 Pixiv 远端状态时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '作品首次归档时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '当前作品记录最后更新时间',
  PRIMARY KEY (id),
  KEY idx_type_status (type,status),
  KEY idx_author_id (author_id),
  KEY idx_remote_update_at (remote_update_at),
  KEY idx_series_lookup (series_id, series_order),
  CONSTRAINT fk_works_series FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='作品当前状态与当前版本元数据';

CREATE TABLE work_history (
  history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '历史版本本地主键',
  work_id VARCHAR(32) NOT NULL COMMENT '关联的 Pixiv 作品 ID',
  version_num INT UNSIGNED NOT NULL COMMENT '作品本地不可变版本号',
  version_token CHAR(64) NOT NULL COMMENT '该历史版本内容指纹 SHA-256',
  title VARCHAR(255) NOT NULL COMMENT '该版本作品标题快照',
  caption TEXT COMMENT '该版本作品简介快照',
  tags_snapshot JSON COMMENT '该版本标签快照 JSON',
  remote_update_at DATETIME NULL COMMENT '该版本对应的 Pixiv 更新时间',
  novel_content MEDIUMTEXT COMMENT '该版本小说正文快照，仅小说使用',
  raw_meta JSON NOT NULL COMMENT '该版本 Pixiv 原始元数据 JSON',
  storage_path VARCHAR(1000) NOT NULL COMMENT '该版本归档目录路径',
  archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '该版本完成归档时间',
  PRIMARY KEY (history_id),
  UNIQUE KEY uk_work_version (work_id, version_num),
  CONSTRAINT fk_wh_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='作品不可变历史版本';

CREATE TABLE work_files (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '当前文件记录本地主键',
  work_id VARCHAR(32) NOT NULL COMMENT '关联的 Pixiv 作品 ID',
  page_index INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '作品页序号或文件逻辑序号',
  file_type ENUM('image','video','ugoira_zip','ugoira_meta','ugoira_mp4','novel_txt','cover') NOT NULL COMMENT '归档文件类型',
  file_path VARCHAR(1000) NOT NULL COMMENT '当前版本文件本地路径',
  file_size BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '文件大小，单位字节',
  file_hash CHAR(64) COMMENT '文件内容 SHA-256',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '当前文件记录创建时间',
  PRIMARY KEY (id),
  KEY idx_work_id (work_id),
  CONSTRAINT fk_wf_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='作品当前版本文件索引';

CREATE TABLE work_history_files (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '历史文件记录本地主键',
  history_id BIGINT UNSIGNED NOT NULL COMMENT '关联的历史版本主键',
  page_index INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '作品页序号或文件逻辑序号',
  file_type ENUM('image','video','ugoira_zip','ugoira_meta','ugoira_mp4','novel_txt','cover') NOT NULL COMMENT '归档文件类型',
  backup_path VARCHAR(1000) NOT NULL COMMENT '历史版本文件本地路径',
  file_size BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '文件大小，单位字节',
  file_hash CHAR(64) COMMENT '文件内容 SHA-256',
  remote_url TEXT COMMENT '归档时对应的 Pixiv 远端素材 URL',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '历史文件记录创建时间',
  PRIMARY KEY (id),
  KEY idx_history_id (history_id),
  CONSTRAINT fk_whf_history_id FOREIGN KEY (history_id) REFERENCES work_history(history_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='作品历史版本文件索引';

CREATE TABLE work_tag_relation (
  work_id VARCHAR(32) NOT NULL COMMENT 'Pixiv 作品 ID',
  tag_id INT UNSIGNED NOT NULL COMMENT '标签本地主键',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '作品与标签关系创建时间',
  PRIMARY KEY (work_id,tag_id),
  KEY idx_tag_id (tag_id),
  CONSTRAINT fk_wtr_tag_id FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE,
  CONSTRAINT fk_wtr_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='作品与标签多对多关系';

CREATE TABLE sync_sources (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '同步源本地主键',
  type ENUM('author','bookmarks') NOT NULL COMMENT '同步源类型：作者作品或收藏',
  remote_user_id VARCHAR(32) NOT NULL COMMENT 'Pixiv 用户 ID；self 表示当前认证账号',
  restrict_mode ENUM('public','private') NOT NULL DEFAULT 'public' COMMENT '收藏可见范围：公开或私密',
  include_illust TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否同步插画/漫画/Ugoira',
  include_novel TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否同步小说',
  enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '同步源是否启用',
  interval_seconds INT UNSIGNED NOT NULL DEFAULT 21600 COMMENT '自动同步周期，单位秒',
  frontier_json JSON COMMENT '上次成功同步保存的增量边界 JSON',
  next_run_at DATETIME COMMENT '下一次计划执行时间',
  last_run_at DATETIME COMMENT '最近一次开始执行时间',
  last_success_at DATETIME COMMENT '最近一次成功执行时间',
  last_error VARCHAR(1000) COMMENT '最近一次同步错误摘要',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '同步源创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '同步源最后更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sync_source_identity (type, remote_user_id, restrict_mode),
  KEY idx_sync_due (enabled, next_run_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='作者或收藏的持久化自动同步配置';

CREATE TABLE work_sources (
  work_id VARCHAR(32) NOT NULL COMMENT 'Pixiv 作品 ID',
  source_id BIGINT UNSIGNED NOT NULL COMMENT '发现该作品的同步源 ID',
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '该同步源首次发现作品时间',
  last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '该同步源最近一次发现作品时间',
  PRIMARY KEY (work_id, source_id),
  KEY idx_work_sources_source (source_id, last_seen_at),
  CONSTRAINT fk_ws_work_id FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
  CONSTRAINT fk_ws_source_id FOREIGN KEY (source_id) REFERENCES sync_sources(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='归档作品与同步来源的多对多追踪关系';
