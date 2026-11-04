-- 手工兼容补丁，适用于 schema 7、8 或 9；不改变 schema_version。
-- 已执行旧版 v8_to_v9.sql 的数据库也需检查并执行本补丁。
-- 先备份并停止全部应用和 worker，明确选择目标数据库；禁止使用 --force。
-- 只补缺失列及来源枚举，不改来源 ID/type/归因、作品记录或磁盘文件。
-- 可重复执行；MySQL DDL 隐式提交，遇错应停止并核对已完成的步骤。
DELIMITER $$
CREATE PROCEDURE pixiv_sync_sources_compat()
BEGIN
  IF NOT EXISTS (SELECT 1 FROM schema_version WHERE id=1 AND version IN (7,8,9)) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='sync source compatibility patch requires schema_version 7, 8 or 9';
  END IF;
  IF EXISTS (SELECT 1 FROM sync_sources
      WHERE CAST(type AS CHAR) NOT IN ('author','bookmarks','novel_series','manga_series','followed_series','watchlist')) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Unknown sync source type; review before changing the enum';
  END IF;
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
  ALTER TABLE sync_sources
    MODIFY COLUMN type ENUM('author','bookmarks','novel_series','manga_series','followed_series','watchlist') NOT NULL COMMENT '同步源类型：作者、收藏、小说合集、漫画合集、追更清单或兼容旧小说追更',
    MODIFY COLUMN remote_user_id VARCHAR(32) NOT NULL COMMENT '远端用户或合集 ID；self 表示相应凭据的当前账号';
END$$
DELIMITER ;
CALL pixiv_sync_sources_compat();
DROP PROCEDURE pixiv_sync_sources_compat;

SELECT id, version FROM schema_version WHERE id=1;
SHOW COLUMNS FROM sync_sources WHERE Field IN ('type','expand_series','scan_state_json');
SELECT type, COUNT(*) AS source_count FROM sync_sources GROUP BY type;
