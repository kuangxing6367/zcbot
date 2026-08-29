-- ============================================================
-- 迁移脚本：为 commands 表添加 alias/description/is_active 列
-- 用于已存在的数据库升级
-- ============================================================

USE zcbot;

-- 添加 alias 列（命令别名，逗号分隔）
ALTER TABLE commands ADD COLUMN IF NOT EXISTS alias VARCHAR(500) DEFAULT NULL COMMENT '命令别名（逗号分隔，如 /help,/h）' AFTER pattern;

-- 添加 description 列（命令描述）
ALTER TABLE commands ADD COLUMN IF NOT EXISTS description VARCHAR(500) DEFAULT NULL COMMENT '命令描述' AFTER alias;

-- 添加 is_active 列（启用/禁用）
ALTER TABLE commands ADD COLUMN IF NOT EXISTS is_active TINYINT(1) DEFAULT 1 COMMENT '启用/禁用' AFTER is_dynamic;

-- 添加索引
ALTER TABLE commands ADD INDEX IF NOT EXISTS idx_active (is_active);
