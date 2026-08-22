-- ============================================================
-- MySQL 初始化脚本（docker-compose 首次启动时自动执行）
-- 库 ticket_agent 由 MYSQL_DATABASE 环境变量自动创建；
-- 用户 ticket_agent 由 MYSQL_USER/MYSQL_PASSWORD 自动创建并授予该库权限。
-- 表结构与 agent/infra.py::ensure_mysql_schema 保持一致（幂等）。
-- ============================================================

-- 工单完整记录
CREATE TABLE IF NOT EXISTS work_order (
    order_id        VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    created_at      VARCHAR(32) NOT NULL,
    intent          VARCHAR(32),
    parsed_ticket   JSON,
    need_list       JSON,
    allow_list      JSON,
    deny_list       JSON,
    evidence_index  JSON,
    rag_results     JSON,
    rag_low_score   TINYINT(1),
    diagnosis_result JSON,
    final_report    LONGTEXT,
    INDEX idx_tenant (tenant_id),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 长期记忆摘要（供后续工单推理参考，按租户隔离）
CREATE TABLE IF NOT EXISTS long_term_memory (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id    VARCHAR(64) NOT NULL,
    tenant_id   VARCHAR(64) NOT NULL,
    created_at  VARCHAR(32) NOT NULL,
    phenomenon  VARCHAR(512),
    service     VARCHAR(128),
    confidence  VARCHAR(16),
    root_causes JSON,
    INDEX idx_tenant (tenant_id),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
