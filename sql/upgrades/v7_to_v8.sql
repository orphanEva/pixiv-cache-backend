-- 手工升级 v7 -> v8。先备份并停止全部应用和 worker，选择目标数据库。
-- MySQL DDL 隐式提交；请用遇错停止的客户端执行，禁止 --force。
-- 保留作品、文件和历史版本；补齐两条 v7 分支的同步配置，再添加账号关联和收藏迁移进度。
DELIMITER $$
CREATE PROCEDURE pixiv_require_schema_v7()
BEGIN
  IF NOT EXISTS (SELECT 1 FROM schema_version WHERE id=1 AND version=7) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='v7_to_v8 requires schema_version=7';
  END IF;
  -- 旧 watchlist 分支的 v7 尚未添加以下两列；本地 v7 已有则原样保留。
  IF NOT EXISTS (SELECT 1 FROM information_schema.COLUMNS
      WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sync_sources' AND COLUMN_NAME='expand_series') THEN
    ALTER TABLE sync_sources ADD COLUMN expand_series TINYINT(1) NOT NULL DEFAULT 0
      COMMENT '收藏扫描是否展开并持续订阅所属合集，默认关闭' AFTER include_novel;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.COLUMNS
      WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sync_sources' AND COLUMN_NAME='scan_state_json') THEN
    ALTER TABLE sync_sources ADD COLUMN scan_state_json JSON
      COMMENT '未完成扫描的分页游标、候选边界和全量模式；完成后清空' AFTER frontier_json;
  END IF;
END$$
DELIMITER ;
CALL pixiv_require_schema_v7();
DROP PROCEDURE pixiv_require_schema_v7;

ALTER TABLE sync_sources
  MODIFY COLUMN type ENUM('author','bookmarks','novel_series','manga_series','followed_series','watchlist') NOT NULL COMMENT '同步源类型：作者、收藏、小说合集、漫画合集、追更清单或兼容旧小说追更',
  MODIFY COLUMN remote_user_id VARCHAR(32) NOT NULL COMMENT '远端用户或合集 ID；self 表示相应凭据的当前账号',
  ADD COLUMN account_id VARCHAR(32) COMMENT 'self 来源分页进度所属的已验证账号 ID' AFTER scan_state_json;

CREATE TABLE account_migration_state (
  id INT NOT NULL COMMENT '固定主键，应用使用 1',
  active_user_id VARCHAR(32) NOT NULL COMMENT '当前允许执行迁移的 Pixiv 账号 ID',
  activated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '最近一次账号激活时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='跨进程账号迁移协调状态';

CREATE TABLE account_migration_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '收藏迁移任务本地主键',
  target_user_id VARCHAR(32) NOT NULL COMMENT '目标 Pixiv 账号 ID',
  automatic_key VARCHAR(32) COMMENT '自动任务按账号去重，手动任务为空',
  status VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT '任务状态：等待、执行、暂停或完成',
  scan_phase VARCHAR(16) NOT NULL DEFAULT 'works' COMMENT '本地清单扫描阶段：作品、合集、订阅或完成',
  scan_cursor JSON COMMENT '本地清单分页扫描位置',
  last_error VARCHAR(1000) COMMENT '最近一次安全错误摘要',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '任务创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '任务最后更新时间',
  finished_at DATETIME COMMENT '任务结束时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_account_migration_automatic (automatic_key),
  KEY idx_account_migration_runs_target (target_user_id,status,id),
  CONSTRAINT chk_account_run_status CHECK (status IN ('pending','running','paused','completed')),
  CONSTRAINT chk_account_run_phase CHECK (scan_phase IN ('works','series','sources','done'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='按账号持久化的作品收藏与合集追更迁移任务';

CREATE TABLE account_migration_items (
  run_id BIGINT UNSIGNED NOT NULL COMMENT '所属迁移任务 ID',
  kind VARCHAR(16) NOT NULL COMMENT '迁移对象类型：插画、小说、漫画合集或小说合集',
  target_id VARCHAR(32) NOT NULL COMMENT 'Pixiv 作品或合集 ID',
  status VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT '项目状态：等待、执行、成功、失败或跳过',
  attempts INT NOT NULL DEFAULT 0 COMMENT '远端操作已尝试次数',
  next_attempt_at DATETIME COMMENT '允许下次重试的时间',
  last_error_type VARCHAR(64) COMMENT '最近一次错误类型',
  last_error VARCHAR(1000) COMMENT '最近一次安全错误摘要',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '项目首次发现时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '项目最后更新时间',
  finished_at DATETIME COMMENT '项目结束时间',
  PRIMARY KEY (run_id,kind,target_id),
  KEY idx_account_migration_items_due (run_id,status,next_attempt_at),
  CONSTRAINT fk_account_migration_item_run FOREIGN KEY (run_id) REFERENCES account_migration_runs(id) ON DELETE CASCADE,
  CONSTRAINT chk_account_item_kind CHECK (kind IN ('illust','novel','series_manga','series_novel')),
  CONSTRAINT chk_account_item_status CHECK (status IN ('pending','running','succeeded','failed','skipped'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='迁移项目的重试、幂等与失败记录';

UPDATE schema_version SET version=8 WHERE id=1 AND version=7;
