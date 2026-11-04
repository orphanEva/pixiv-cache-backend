-- 手工升级 v8 -> v9，MySQL 8.0+；先备份并停止全部应用和 worker。
-- 选择目标数据库后执行；DDL 隐式提交，必须遇错停止，禁止 --force。
-- 本脚本保留原 ID、历史版本及磁盘路径，为作品及关联表补上命名空间。
DELIMITER $$
CREATE PROCEDURE pixiv_require_schema_v8()
BEGIN
  IF NOT EXISTS (SELECT 1 FROM schema_version WHERE id=1 AND version=8) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='v8_to_v9 requires schema_version=8';
  END IF;
END$$
DELIMITER ;
CALL pixiv_require_schema_v8();
DROP PROCEDURE pixiv_require_schema_v8;

-- 保留原有 watchlist 行，并兼容先前没有该枚举值的本地 v8。
ALTER TABLE sync_sources
  MODIFY COLUMN type ENUM('author','bookmarks','novel_series','manga_series','followed_series','watchlist') NOT NULL COMMENT '同步源类型：作者、收藏、小说合集、漫画合集、追更清单或兼容旧小说追更';

ALTER TABLE works
  ADD COLUMN work_kind ENUM('illust','novel') NULL COMMENT '作品身份命名空间，漫画和动图属于插画' FIRST,
  ADD COLUMN next_recheck_at DATETIME NULL COMMENT '旧作品远端复查任务的下次允许投递时间' AFTER last_checked_at;
UPDATE works SET work_kind=IF(type='novel','novel','illust');

ALTER TABLE work_history ADD COLUMN work_kind ENUM('illust','novel') NULL COMMENT '关联作品身份命名空间' AFTER history_id;
ALTER TABLE work_files ADD COLUMN work_kind ENUM('illust','novel') NULL COMMENT '关联作品身份命名空间' AFTER id;
ALTER TABLE work_tag_relation ADD COLUMN work_kind ENUM('illust','novel') NULL COMMENT '关联作品身份命名空间' FIRST;
ALTER TABLE work_sources ADD COLUMN work_kind ENUM('illust','novel') NULL COMMENT '关联作品身份命名空间' FIRST;
UPDATE work_history h JOIN works w ON h.work_id=w.id SET h.work_kind=w.work_kind;
UPDATE work_files f JOIN works w ON f.work_id=w.id SET f.work_kind=w.work_kind;
UPDATE work_tag_relation t JOIN works w ON t.work_id=w.id SET t.work_kind=w.work_kind;
UPDATE work_sources s JOIN works w ON s.work_id=w.id SET s.work_kind=w.work_kind;

ALTER TABLE work_history DROP FOREIGN KEY fk_wh_work_id;
ALTER TABLE work_files DROP FOREIGN KEY fk_wf_work_id;
ALTER TABLE work_tag_relation DROP FOREIGN KEY fk_wtr_work_id;
ALTER TABLE work_sources DROP FOREIGN KEY fk_ws_work_id;
ALTER TABLE works
  MODIFY COLUMN work_kind ENUM('illust','novel') NOT NULL COMMENT '作品身份命名空间，漫画和动图属于插画',
  DROP PRIMARY KEY, ADD PRIMARY KEY (work_kind,id),
  ADD KEY idx_work_recheck (status,next_recheck_at,last_checked_at),
  ADD CONSTRAINT chk_work_namespace CHECK ((work_kind='novel' AND type='novel') OR (work_kind='illust' AND type IN ('illust','manga','ugoira')));
ALTER TABLE work_history
  MODIFY COLUMN work_kind ENUM('illust','novel') NOT NULL COMMENT '关联作品身份命名空间',
  DROP INDEX uk_work_version, ADD UNIQUE KEY uk_work_version (work_kind,work_id,version_num),
  ADD CONSTRAINT fk_wh_work_id FOREIGN KEY (work_kind,work_id) REFERENCES works(work_kind,id) ON DELETE CASCADE;
ALTER TABLE work_files
  MODIFY COLUMN work_kind ENUM('illust','novel') NOT NULL COMMENT '关联作品身份命名空间',
  DROP INDEX uk_work_file_identity, ADD UNIQUE KEY uk_work_file_identity (work_kind,work_id,page_index,file_type),
  ADD CONSTRAINT fk_wf_work_id FOREIGN KEY (work_kind,work_id) REFERENCES works(work_kind,id) ON DELETE CASCADE;
ALTER TABLE work_tag_relation
  MODIFY COLUMN work_kind ENUM('illust','novel') NOT NULL COMMENT '关联作品身份命名空间',
  DROP PRIMARY KEY, ADD PRIMARY KEY (work_kind,work_id,tag_id),
  ADD CONSTRAINT fk_wtr_work_id FOREIGN KEY (work_kind,work_id) REFERENCES works(work_kind,id) ON DELETE CASCADE;
ALTER TABLE work_sources
  MODIFY COLUMN work_kind ENUM('illust','novel') NOT NULL COMMENT '关联作品身份命名空间',
  DROP PRIMARY KEY, ADD PRIMARY KEY (work_kind,work_id,source_id),
  ADD CONSTRAINT fk_ws_work_id FOREIGN KEY (work_kind,work_id) REFERENCES works(work_kind,id) ON DELETE CASCADE;

-- 旧清单游标尚不含命名空间，重新发现未完成任务；逐项成功记录保留并去重。
UPDATE account_migration_runs SET scan_phase='works',scan_cursor=JSON_OBJECT(),updated_at=CURRENT_TIMESTAMP
WHERE status IN ('pending','running','paused');
UPDATE schema_version SET version=9 WHERE id=1 AND version=8;
