"""agent/persistence.py
========================
工单持久化与长期记忆。

- save_work_order：每次诊断完成，把 state 关键内容序列化为 JSON，写入
  ./work_order_history/<order_id>.json；同时追加一条摘要到 long_term_memory.jsonl。
- load_long_term_memory：读取最近 N 条历史工单摘要，供诊断节点作参考提示
  （仅作上下文提示，禁止直接复用历史根因）。

目录布局：
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

LANGGRAPH_DEMO_DIR: Path = Path(__file__).resolve().parent.parent
WORK_ORDER_HISTORY_DIR: Path = LANGGRAPH_DEMO_DIR / "work_order_history"
LONG_TERM_MEMORY_FILE: Path = WORK_ORDER_HISTORY_DIR / "long_term_memory.jsonl"


def ensure_dirs() -> None:
    """确保历史工单输出目录存在。"""
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


def save_work_order(state: dict[str, Any]) -> str:
    """把 state 关键内容序列化为 JSON 落盘，并追加长期记忆摘要，返回 order_id。"""
    ensure_dirs()
    order_id = state.get("order_id") or new_order_id()

    record = {
        "order_id": order_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "intent": state.get("intent"),
        "parsed_ticket": state.get("parsed_ticket", {}),
        "need_data_source_list": state.get("need_data_source_list", []),
        "user_allow_list": state.get("user_allow_list", []),
        "user_deny_list": state.get("user_deny_list", []),
        "evidence_index": _evidence_index(state.get("evidence", {})),
        "rag_results": state.get("rag_results", []),
        "rag_low_score": state.get("rag_low_score", False),
        "diagnosis_result": state.get("diagnosis_result", {}),
        "final_report_markdown": state.get("final_report_markdown", ""),
    }
    out_path = WORK_ORDER_HISTORY_DIR / f"{order_id}.json"
    out_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 追加长期记忆摘要（仅关键字段，供后续工单参考）
    diag = state.get("diagnosis_result", {}) or {}
    parsed = state.get("parsed_ticket", {}) or {}
    root_causes = diag.get("root_causes", [])
    summary = {
        "order_id": order_id,
        "created_at": record["created_at"],
        "phenomenon": parsed.get("phenomenon", ""),
        "service": parsed.get("service", ""),
        "confidence": diag.get("confidence", ""),
        "root_causes": [
            rc.get("cause", "") if isinstance(rc, dict) else str(rc)
            for rc in root_causes
        ],
    }
    with LONG_TERM_MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    return order_id


def load_long_term_memory(limit: int = 5) -> list[dict]:
    """读取最近 limit 条历史工单摘要（不足则返回全部）。"""
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
    return out
