-- v4 -> v5：请先备份，检查重复文件记录后手工执行。
-- 重复文件预检（如存在记录请先人工确认保留哪条，勿自动删除）：
-- SELECT work_id,page_index,file_type,COUNT(*) c FROM work_files GROUP BY work_id,page_index,file_type HAVING c>1;
-- SELECT history_id,page_index,file_type,COUNT(*) c FROM work_history_files GROUP BY history_id,page_index,file_type HAVING c>1;

CREATE TABLE IF NOT EXISTS series_members (
  series_id VARCHAR(32) NOT NULL COMMENT '系列 ID，关联 series.id',
  work_id VARCHAR(32) NOT NULL COMMENT '系列作品 Pixiv ID，可先于归档作品记录存在',
  work_type ENUM('novel','manga') NOT NULL COMMENT '成员作品类别',
  series_order INT UNSIGNED COMMENT '系列作品序号',
  status ENUM('discovered','queued','archived','failed') NOT NULL DEFAULT 'discovered' COMMENT '发现、排队、归档成功或最终失败',
  last_error VARCHAR(1000) COMMENT '最后一次终态错误的安全摘要',
  discovered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次发现时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '成员状态最后更新时间',
  PRIMARY KEY (series_id,work_id),
  KEY idx_series_members_status (series_id,status),
  KEY idx_series_members_work (work_id),
  CONSTRAINT fk_series_members_series FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='系列成员的归档进度';

ALTER TABLE work_files ADD UNIQUE KEY uk_work_file_identity (work_id,page_index,file_type);
ALTER TABLE work_history_files ADD UNIQUE KEY uk_history_file_identity (history_id,page_index,file_type);
UPDATE schema_version SET version=5 WHERE id=1 AND version=4;
