"""agent/infra.py
=================
基础设施客户端封装：Milvus / MySQL / Redis + LLM 限流 + 调用日志 + 重试 + 租户解析。

工程边界补齐（对应「补齐工程边界」需求）：
- 租户隔离：所有外部查询入口强制 tenant_id（缺失退化为 'default'），日志/落库均带 tenant；
- 超时 + 重试：LLM 与工具调用统一走 with_retry（指数退避，可配超时），避免单点抖动拖垮流程；
- 限流：LLM 调用按 tenant 维度计数（Redis 优先，无 Redis 退化为进程内滑窗），防 API 被打爆；
- 异常降级：所有 get_xxx() 连接失败返回 None，调用方据此降级，绝不抛堆栈中断主流程；
- 调用日志：log_call() 记录 component / tenant / prompt / 工具入参出参 / token 消耗，
  写入 logs/calls.jsonl，便于事后复盘与作品集演示。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 路径常量
# 本文件位于 <repo_root>/langgraph_demo/agent/infra.py
# ---------------------------------------------------------------------------
LANGGRAPH_DEMO_DIR: Path = Path(__file__).resolve().parent.parent  # langgraph_demo/
LOGS_DIR: Path = LANGGRAPH_DEMO_DIR / "logs"
CALL_LOG_FILE: Path = LOGS_DIR / "calls.jsonl"

# 默认租户（POC 阶段：所有样本数据归属 default 租户，真实系统由登录态注入）
DEFAULT_TENANT = "default"

# 连接单例（懒加载；None 表示不可用，调用方降级）
_redis_client = None
_mysql_conn = None
_milvus_client = None

# 进程内限流退化计数：tenant -> [时间戳...]
_inproc_rate: dict[str, list[float]] = {}
# 调用日志写入锁（多线程/异步并发安全）
_log_lock = threading.Lock()


# ===========================================================================
# 1. 租户解析
# ===========================================================================
def resolve_tenant(tenant_id: str | None = None) -> str:
    """解析当前请求的 tenant_id：参数 > 环境变量 > default。"""
    t = (tenant_id or os.getenv("TENANT_ID") or DEFAULT_TENANT).strip()
    return t or DEFAULT_TENANT


# ===========================================================================
# 2. 调用日志
# ===========================================================================
def _trunc(x: Any, n: int = 4000) -> str:
    """把任意对象转成字符串并截断，避免单条日志撑爆文件。"""
    if x is None:
        return ""
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "...(truncated)"


def log_call(
    component: str,
    *,
    tenant_id: str = "",
    prompt: str = "",
    input_params: Any = None,
    output: Any = None,
    tokens: dict | None = None,
    error: str = "",
    extra: dict | None = None,
) -> None:
    """记录一次调用（LLM / MCP / RAG / 持久化），写 logs/calls.jsonl。

    永不抛异常——日志失败不能影响主流程。
    """
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "component": component,
            "tenant_id": tenant_id or DEFAULT_TENANT,
            "prompt": _trunc(prompt),
            "input_params": _trunc(input_params, 2000),
            "output": _trunc(output),
            "tokens": tokens or {},
            "error": error,
            "extra": extra or {},
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _log_lock:
            with CALL_LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        print(
            f"  [调用日志] {component} tenant={rec['tenant_id']} "
            f"tokens={tokens or '-'} err={'Y' if error else 'N'}",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001  日志失败静默
        pass


def extract_tokens(resp: Any) -> dict:
    """从 LangChain AIMessage 提取 token 消耗（兼容 usage_metadata / response_metadata）。"""
    if resp is None:
        return {}
    um = getattr(resp, "usage_metadata", None)
    if isinstance(um, dict) and um:
        return {
            "input": um.get("input_tokens") or um.get("prompt_tokens"),
            "output": um.get("output_tokens") or um.get("completion_tokens"),
            "total": um.get("total_tokens"),
        }
    rm = getattr(resp, "response_metadata", None) or {}
    tu = (rm.get("token_usage") or {}) if isinstance(rm, dict) else {}
    if tu:
        return {
            "input": tu.get("prompt_tokens"),
            "output": tu.get("completion_tokens"),
            "total": tu.get("total_tokens"),
        }
    return {}


# ===========================================================================
# 3. 重试（指数退避 + 可选超时）
# ===========================================================================
async def with_retry(
    coro_fn: Callable[[], Any],
    *,
    retries: int = 2,
    base_delay: float = 1.0,
    timeout: float | None = None,
    label: str = "call",
) -> Any:
    """对异步调用做重试（指数退避），可叠加超时。

    - coro_fn：无参函数，每次调用返回新协程（保证可重试）；
    - timeout：单次调用超时秒数，超时计入重试次数；
    - 全部失败则抛出最后一次异常，由调用方降级。
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if timeout is not None:
                return await asyncio.wait_for(coro_fn(), timeout=timeout)
            return await coro_fn()
        except asyncio.TimeoutError as exc:
            last_exc = TimeoutError(f"{label} 超时({timeout}s)")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        if attempt < retries:
            await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


# ===========================================================================
# 4. LLM 限流（Redis 优先，进程内退化）
# ===========================================================================
def llm_rate_allowed(tenant_id: str) -> tuple[bool, str]:
    """判断该租户当前是否仍可调用 LLM。

    - Redis 可用：INCR + EXPIRE 滑窗（多进程一致）；
    - Redis 不可用：进程内滑窗退化（仅单进程准确，Demo 足够）；
    返回 (是否允许, 拒绝原因)。
    """
    limit = int(os.getenv("LLM_RATE_LIMIT", "30"))   # 每窗口最大调用次数
    window = int(os.getenv("LLM_RATE_WINDOW", "60"))  # 窗口秒数
    tenant = resolve_tenant(tenant_id)
    key = f"llm_rate:{tenant}"

    r = get_redis()
    if r is not None:
        try:
            cnt = r.incr(key)
            if cnt == 1:
                r.expire(key, window)
            if cnt > limit:
                return False, f"LLM 限流触发（>{limit}/{window}s）"
            return True, ""
        except Exception:  # noqa: BLE001  Redis 故障退化进程内
            pass

    now = time.time()
    arr = [t for t in _inproc_rate.get(key, []) if now - t < window]
    arr.append(now)
    _inproc_rate[key] = arr
    if len(arr) > limit:
        return False, f"LLM 限流触发（>{limit}/{window}s，进程内退化）"
    return True, ""


# ===========================================================================
# 5. Redis（限流 / 会话缓存 / 长期记忆缓存）
# ===========================================================================
def get_redis():
    """返回 redis 客户端，连接失败返回 None（调用方降级）。"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis  # 延迟 import，避免未装依赖时 import 失败

        _redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD", "") or None,
            db=int(os.getenv("REDIS_DB", "0")),
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
        _redis_client.ping()
    except Exception:  # noqa: BLE001
        _redis_client = None
    return _redis_client


# ===========================================================================
# 6. MySQL（工单持久化 + 长期记忆）
# ===========================================================================
def get_mysql():
    """返回 MySQL 连接，失败返回 None（调用方降级为 JSON 落盘）。"""
    global _mysql_conn
    if _mysql_conn is not None:
        try:
            _mysql_conn.ping(reconnect=True)
            return _mysql_conn
        except Exception:  # noqa: BLE001
            _mysql_conn = None
    try:
        import pymysql  # 延迟 import

        _mysql_conn = pymysql.connect(
            host=os.getenv("MYSQL_HOST", "localhost"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "ticket_agent"),
            charset="utf8mb4",
            connect_timeout=3,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception:  # noqa: BLE001
        _mysql_conn = None
    return _mysql_conn


def ensure_mysql_schema() -> bool:
    """建表（幂等）。成功返回 True，MySQL 不可用返回 False。"""
    conn = get_mysql()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
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
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
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
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# 7. Milvus（向量库）
# ===========================================================================
def get_milvus():
    """返回 Milvus 客户端，连接失败返回 None（调用方降级跳过 RAG）。"""
    global _milvus_client
    if _milvus_client is not None:
        return _milvus_client
    try:
        from pymilvus import MilvusClient  # 延迟 import

        _milvus_client = MilvusClient(
            uri=os.getenv("MILVUS_URI", "http://localhost:19530"),
            token=os.getenv("MILVUS_TOKEN", "") or None,
            timeout=float(os.getenv("MILVUS_TIMEOUT", "5")),
        )
        # 触发一次连接验证
        _milvus_client.list_collections()
    except Exception:  # noqa: BLE001
        _milvus_client = None
    return _milvus_client


def infra_status() -> dict[str, bool]:
    """供启动自检：探测各基础设施可用性（不抛异常）。"""
    return {
        "milvus": get_milvus() is not None,
        "mysql": get_mysql() is not None,
        "redis": get_redis() is not None,
    }
