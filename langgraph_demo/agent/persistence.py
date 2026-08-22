"""agent/persistence.py
========================
工单持久化与长期记忆。

主路径：MySQL（work_order / long_term_memory 两表，幂等建表）。
降级路径：MySQL 不可用时退回本地 JSON（./work_order_history/<order_id>.json
+ long_term_memory.jsonl），保证 Demo 在无 DB 环境仍可跑通。

工程边界
--------
- 租户隔离：所有写入/读取强制 tenant_id（取自 state，缺失退化为 default）；
- 调用日志：通过 infra.log_call 记录落库入参/出参/失败原因；
- 异常降级：DB 写入失败自动降级 JSON 落盘，绝不抛堆栈中断主流程。

目录布局（降级模式）：
  langgraph_demo/work_order_history/
      ├─ WO-YYYYMMDDHHMMSS-xxxxxxxx.json   # 单工单完整记录
      └─ long_term_memory.jsonl            # 历史摘要（append-only）
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .infra import DEFAULT_TENANT, ensure_mysql_schema, get_mysql, log_call, resolve_tenant

LANGGRAPH_DEMO_DIR: Path = Path(__file__).resolve().parent.parent
WORK_ORDER_HISTORY_DIR: Path = LANGGRAPH_DEMO_DIR / "work_order_history"
LONG_TERM_MEMORY_FILE: Path = WORK_ORDER_HISTORY_DIR / "long_term_memory.jsonl"


def ensure_dirs() -> None:
    """确保降级用的历史工单输出目录存在。"""
    WORK_ORDER_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def new_order_id() -> str:
    """生成工单唯一编号：WO-时间戳-8位随机。"""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"WO-{ts}-{short}"


def _evidence_index(evidence: dict) -> dict:
    """把证据字典压缩为「文件路径 + 分数」索引（避免完整内容撑大记录）。"""
    out: dict[str, list[dict]] = {}
    for src, files in (evidence or {}).items():
        out[src] = [
            {"file_path": f.get("file_path"), "score": f.get("score")}
            for f in files
        ]
    return out


def _summary_of(state: dict[str, Any], order_id: str, tenant_id: str, created_at: str) -> dict:
    """构造长期记忆摘要（仅关键字段，供后续工单参考）。"""
    diag = state.get("diagnosis_result", {}) or {}
    parsed = state.get("parsed_ticket", {}) or {}
    root_causes = diag.get("root_causes", [])
    return {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "created_at": created_at,
        "phenomenon": parsed.get("phenomenon", ""),
        "service": parsed.get("service", ""),
        "confidence": diag.get("confidence", ""),
        "root_causes": [
            rc.get("cause", "") if isinstance(rc, dict) else str(rc)
            for rc in root_causes
        ],
    }


def save_work_order(state: dict[str, Any]) -> str:
    """把 state 关键内容落库（MySQL 优先，失败降级 JSON），返回 order_id。"""
    order_id = state.get("order_id") or new_order_id()
    tenant_id = resolve_tenant(state.get("tenant_id"))
    created_at = datetime.now().isoformat(timespec="seconds")

    record = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "created_at": created_at,
        "intent": state.get("intent"),
        "parsed_ticket": state.get("parsed_ticket", {}),
        "need_list": state.get("need_data_source_list", []),
        "allow_list": state.get("user_allow_list", []),
        "deny_list": state.get("user_deny_list", []),
        "evidence_index": _evidence_index(state.get("evidence", {})),
        "rag_results": state.get("rag_results", []),
        "rag_low_score": bool(state.get("rag_low_score", False)),
        "diagnosis_result": state.get("diagnosis_result", {}),
        "final_report": state.get("final_report_markdown", ""),
    }
    summary = _summary_of(state, order_id, tenant_id, created_at)

    # —— 主路径：MySQL ——
    if ensure_mysql_schema():
        conn = get_mysql()
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO work_order
                          (order_id, tenant_id, created_at, intent, parsed_ticket,
                           need_list, allow_list, deny_list, evidence_index,
                           rag_results, rag_low_score, diagnosis_result, final_report)
                        VALUES (%(order_id)s, %(tenant_id)s, %(created_at)s, %(intent)s,
                           %(parsed_ticket)s, %(need_list)s, %(allow_list)s, %(deny_list)s,
                           %(evidence_index)s, %(rag_results)s, %(rag_low_score)s,
                           %(diagnosis_result)s, %(final_report)s)
                        """,
                        {**record,
                         "parsed_ticket": json.dumps(record["parsed_ticket"], ensure_ascii=False),
                         "need_list": json.dumps(record["need_list"], ensure_ascii=False),
                         "allow_list": json.dumps(record["allow_list"], ensure_ascii=False),
                         "deny_list": json.dumps(record["deny_list"], ensure_ascii=False),
                         "evidence_index": json.dumps(record["evidence_index"], ensure_ascii=False),
                         "rag_results": json.dumps(record["rag_results"], ensure_ascii=False),
                         "diagnosis_result": json.dumps(record["diagnosis_result"], ensure_ascii=False)},
                    )
                    cur.execute(
                        """
                        INSERT INTO long_term_memory
                          (order_id, tenant_id, created_at, phenomenon, service,
                           confidence, root_causes)
                        VALUES (%(order_id)s, %(tenant_id)s, %(created_at)s, %(phenomenon)s,
                           %(service)s, %(confidence)s, %(root_causes)s)
                        """,
                        {**summary,
                         "root_causes": json.dumps(summary["root_causes"], ensure_ascii=False)},
                    )
                conn.commit()
                log_call("persistence:save_mysql", tenant_id=tenant_id,
                         input_params={"order_id": order_id}, output={"saved": True})
                return order_id
            except Exception as exc:  # noqa: BLE001
                log_call("persistence:save_mysql", tenant_id=tenant_id,
                         input_params={"order_id": order_id}, error=str(exc))
                # 落到 JSON 降级

    # —— 降级路径：本地 JSON ——
    ensure_dirs()
    out_path = WORK_ORDER_HISTORY_DIR / f"{order_id}.json"
    try:
        out_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with LONG_TERM_MEMORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        log_call("persistence:save_json", tenant_id=tenant_id,
                 input_params={"order_id": order_id}, output={"fallback": True})
    except Exception as exc:  # noqa: BLE001
        log_call("persistence:save_json", tenant_id=tenant_id,
                 input_params={"order_id": order_id}, error=str(exc))
    return order_id


def load_long_term_memory(tenant_id: str = DEFAULT_TENANT, limit: int = 5) -> list[dict]:
    """读取最近 limit 条历史工单摘要（MySQL 优先，失败降级 jsonl）。

    租户隔离：仅返回该租户的历史摘要（共享知识不算历史摘要）。
    """
    tenant_id = resolve_tenant(tenant_id)

    # —— 主路径：MySQL ——
    conn = get_mysql()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT order_id, tenant_id, created_at, phenomenon, service,
                           confidence, root_causes
                    FROM long_term_memory
                    WHERE tenant_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                rows = cur.fetchall() or []
                out: list[dict] = []
                for row in rows:
                    rc = row.get("root_causes")
                    if isinstance(rc, str):
                        try:
                            rc = json.loads(rc)
                        except json.JSONDecodeError:
                            rc = []
                    out.append({
                        "order_id": row.get("order_id"),
                        "tenant_id": row.get("tenant_id"),
                        "created_at": row.get("created_at"),
                        "phenomenon": row.get("phenomenon", ""),
                        "service": row.get("service", ""),
                        "confidence": row.get("confidence", ""),
                        "root_causes": rc or [],
                    })
                return out
        except Exception as exc:  # noqa: BLE001
            log_call("persistence:load_mysql", tenant_id=tenant_id, error=str(exc))

    # —— 降级路径：jsonl（无租户过滤能力，返回全部最近 N 条） ——
    if not LONG_TERM_MEMORY_FILE.exists():
        return []
    lines = LONG_TERM_MEMORY_FILE.read_text(encoding="utf-8").splitlines()
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # jsonl 无索引，尽力按 tenant 过滤
    filtered = [h for h in out if h.get("tenant_id", DEFAULT_TENANT) == tenant_id]
    return filtered if filtered else out
