-- 手工升级 v6 -> v7。请先备份数据库，停止全部应用和 worker。
-- 仅适用于当前版本严格为 6 的数据库；MySQL DDL 隐式提交，不能依赖事务回滚。
-- 执行前先检查：SELECT version FROM schema_version WHERE id=1;
-- 脚本只新增同步配置字段和枚举值，不删除任何作品、历史版本或归档文件。
-- 请使用遇到错误立即停止的 SQL 客户端；不要使用 --force。

DELIMITER $$
CREATE PROCEDURE pixiv_require_schema_v6()
BEGIN
  IF NOT EXISTS (SELECT 1 FROM schema_version WHERE id=1 AND version=6) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='v6_to_v7 requires schema_version=6';
  END IF;
END$$
DELIMITER ;
CALL pixiv_require_schema_v6();
DROP PROCEDURE pixiv_require_schema_v6;

ALTER TABLE sync_sources
  MODIFY COLUMN type ENUM('author','bookmarks','novel_series','manga_series','followed_series','watchlist') NOT NULL COMMENT '同步源类型：作者、收藏、小说合集、漫画合集、追更清单或兼容旧小说追更',
  MODIFY COLUMN remote_user_id VARCHAR(32) NOT NULL COMMENT '远端用户或合集 ID；self 表示相应凭据的当前账号',
  ADD COLUMN expand_series TINYINT(1) NOT NULL DEFAULT 0 COMMENT '收藏扫描是否展开并持续订阅所属合集，默认关闭' AFTER include_novel,
  ADD COLUMN scan_state_json JSON COMMENT '未完成扫描的分页游标、候选边界和全量模式；完成后清空' AFTER frontier_json;

UPDATE schema_version SET version=7 WHERE id=1 AND version=6;
