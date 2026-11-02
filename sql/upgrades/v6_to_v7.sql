-- 手工升级：v6 -> v7，仅变更同步来源枚举，保留所有现有数据和文件。
-- 先备份 pixiv_archive、确认 SELECT version FROM schema_version WHERE id=1 返回 6。
-- API / sync / archive / maintenance 等进程必须在执行期间停止。
USE pixiv_archive;

ALTER TABLE sync_sources
    MODIFY COLUMN type ENUM('author','bookmarks','watchlist') NOT NULL
    COMMENT '同步源类型：作者、收藏或追更列表';

UPDATE schema_version SET version = 7 WHERE id = 1 AND version = 6;

-- 必须验证结果 version = 7，且原有同步来源仍存在。
SELECT id, version FROM schema_version;
SELECT id, type, remote_user_id FROM sync_sources ORDER BY id;
