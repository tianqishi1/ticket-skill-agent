"""agent/nodes.py
=================
LangGraph 业务节点实现 + MCP 只读客户端 + LLM 工厂 + 人在回路授权 + RAG 接入。

节点清单
--------
0. router_node                 意图路由（/diagnose 硬路由 或 LLM 识别，低置信度 interrupt 反问）
1. chat_respond_node           闲聊 / 代码问答（不读本地文件）
2. parse_ticket_node           工单解析 + 评估 need_data_source_list + 生成 order_id
3. request_user_authorize_node 申请授权（interrupt 暂停等终端输入）→ 填充 allow/deny
4. collect_evidence_node       仅对 allow_list 调用 MCP 只读工具 / 知识库走 RAG 检索
5. diagnosis_reason_node       综合证据做根因推理（含置信度 + RAG 约束 + 长期记忆参考）
6. generate_report_node        填充固定 Markdown 报告模板（含知识库引用来源）
7. persist_work_order_node     序列化 state 关键内容到 work_order_history/<order_id>.json

安全要点
--------
- LLM 不绑定任何工具，无法自动调用 MCP；MCP 工具仅在 collect_evidence_node 内
  对「已授权」数据源显式调用，从机制上保证「先授权后读取」；
- RAG 检索同属 knowledge_base 数据源，必须用户授权后才执行，未授权直接跳过；
- 修复建议在落报告前经 _sanitize_fix 兜底清洗，移除可直接运行的命令。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from .prompts import (
    CHAT_PROMPT,
    DIAGNOSIS_PROMPT,
    PARSE_TICKET_PROMPT,
    ROUTER_PROMPT,
)
from .state import TicketState

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
LANGGRAPH_DEMO_DIR: Path = Path(__file__).resolve().parent.parent  # langgraph_demo/
REPO_ROOT: Path = LANGGRAPH_DEMO_DIR.parent                        # ticket-skill-agent/
MCP_SERVER_PATH: Path = LANGGRAPH_DEMO_DIR / "mcp_file_server.py"

# ---------------------------------------------------------------------------
# 五类数据源定义：key -> (中文标签, 相对仓库根的访问基准目录)
# ---------------------------------------------------------------------------
DATA_SOURCES: dict[str, dict[str, str]] = {
    "frontend":       {"label": "浏览器前端信息",     "base_dir": "sample_data/frontend_samples"},
    "logs":           {"label": "应用日志与异常堆栈", "base_dir": "sample_data/sample_tickets"},
    "monitor":        {"label": "监控指标数据",       "base_dir": "sample_data/monitor_samples"},
    "code":           {"label": "业务源码逻辑",       "base_dir": "src"},
    "knowledge_base": {"label": "故障知识库",         "base_dir": "sample_data/knowledge_base"},
}

# 每个数据源收集证据时最多读取的文件数（避免上下文爆炸）
MAX_FILES_PER_SOURCE = 10


# ===========================================================================
# LLM 工厂：读取 .env，构造 OpenAI 兼容客户端（适配豆包 Seed / Ark）
# ===========================================================================
def get_llm():
    """根据 .env 配置返回 ChatOpenAI 实例。"""
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("API_KEY")
    if not api_key or api_key.startswith("your_"):
        raise RuntimeError(
            "未检测到有效 API_KEY，请先复制 .env.example 为 .env 并填写豆包 API Key。"
        )
    timeout = int(os.getenv("LLM_TIMEOUT", "60"))
    return ChatOpenAI(
        model=os.getenv("MODEL_NAME", "doubao-seed-1-6-250715"),
        base_url=os.getenv("BASE_URL"),
        api_key=api_key,
        temperature=0.2,
        timeout=timeout,
    )


def _extract_json(text: str) -> dict:
    """从 LLM 输出中容错抽取 JSON 字典。"""
    if not text:
        return {}
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        m = re.search(r"```(\{.*?\})```", text, re.S)
    raw = m.group(1) if m else text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def _safe_get(d: dict, *keys, default: str = "用户未提供") -> str:
    """安全取嵌套字段，缺失返回 default。"""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None or cur == "":
            return default
    return cur if isinstance(cur, str) else str(cur)


# ===========================================================================
# MCP 只读客户端：以 stdio 子进程拉起 mcp_file_server，调用 3 个只读工具
# ===========================================================================
class MCPFileClient:
    """通过 stdio 连接独立 FastMCP 只读文件服务的客户端。"""

    def __init__(self) -> None:
        self._stdio_ctx = None
        self._session_ctx = None
        self._session = None

    async def __aenter__(self) -> "MCPFileClient":
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SERVER_PATH)],
        )
        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        return self

    async def call(self, tool_name: str, **kwargs) -> str:
        """调用一个 MCP 只读工具，返回拼接后的文本结果。"""
        result = await self._session.call_tool(tool_name, kwargs)
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    async def __aexit__(self, *exc):
        try:
            if self._session_ctx is not None:
                await self._session_ctx.__aexit__(*exc)
        finally:
            if self._stdio_ctx is not None:
                await self._stdio_ctx.__aexit__(*exc)


# ===========================================================================
# 节点 0：意图路由 Agent
# ===========================================================================
def _parse_intent_reply(reply: str) -> str:
    """解析用户在「低置信度反问」中的回复 → troubleshoot | chat | code。"""
    text = (reply or "").strip().lower()
    if re.search(r"故障|排查|诊断|工单|diagnose|troubleshoot", text):
        return "troubleshoot"
    if re.search(r"代码|code", text):
        return "code"
    return "chat"


async def router_node(state: TicketState) -> dict:
    """意图路由：/diagnose 前缀硬路由；否则 LLM 识别；置信度 < 0.7 则 interrupt 反问。"""
    from langgraph.types import interrupt

    text = state.get("user_ticket_input", "")
    stripped = text.lstrip()

    if stripped.startswith("/diagnose"):
        # 代码层硬路由：直接进入故障排查子图，不经过 LLM
        intent, conf, reason = "troubleshoot", 1.0, "命中 /diagnose 前缀，代码硬路由"
    else:
        llm = get_llm()
        resp = await llm.ainvoke([
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user", "content": text[:2000]},
        ])
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        try:
            data = _extract_json(raw)
        except Exception:
            data = {}
        intent = str(data.get("intent", "chat")).lower()
        if intent not in ("troubleshoot", "chat", "code"):
            intent = "chat"
        try:
            conf = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        reason = str(data.get("reason", ""))

        # 低置信度 → 人在回路反问确认意图
        if conf < 0.7:
            prompt = (
                f"意图识别置信度较低（{conf:.2f}）：{reason}\n"
                "请确认你的意图：回复【故障排查】/【闲聊】/【代码】（默认按闲聊处理）。"
            )
            user_reply: str = interrupt({"prompt": prompt, "need_list": []})
            intent = _parse_intent_reply(user_reply)
            conf = 1.0
            reason = f"用户已确认意图：{intent}"

    msg = f"路由完成：意图={intent}，置信度={conf:.2f}（{reason}）。"
    return {"intent": intent, "intent_confidence": conf, "messages": [AIMessage(content=msg)]}


def route_after_router(state: TicketState) -> str:
    """条件路由：troubleshoot → 故障排查子图；其余 → 闲聊应答。"""
    return "troubleshoot" if state.get("intent") == "troubleshoot" else "chat"


# ===========================================================================
# 节点 chat：闲聊 / 代码问答（不读本地文件、不调用任何工具）
# ===========================================================================
async def chat_respond_node(state: TicketState) -> dict:
    llm = get_llm()
    text = state.get("user_ticket_input", "")
    resp = await llm.ainvoke([
        {"role": "system", "content": CHAT_PROMPT},
        {"role": "user", "content": text[:4000]},
    ])
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    return {
        "final_report_markdown": content,
        "messages": [AIMessage(content="闲聊 / 代码问答已回复。")],
    }


# ===========================================================================
# 节点 1：工单解析 Agent
# ===========================================================================
async def parse_ticket_node(state: TicketState) -> dict:
    """解析原始工单为结构化字典，评估需要的数据源列表，并生成 order_id。"""
    from .persistence import new_order_id

    ticket_text = state.get("user_ticket_input", "")
    llm = get_llm()
    resp = await llm.ainvoke([
        {"role": "system", "content": PARSE_TICKET_PROMPT},
        {"role": "user", "content": ticket_text},
    ])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)

    try:
        data = _extract_json(raw)
    except Exception:
        data = {
            "parsed_ticket": {"phenomenon": ticket_text[:200]},
            "need_data_source_list": list(DATA_SOURCES.keys()),
        }

    parsed_ticket = data.get("parsed_ticket", {}) or {}
    need_list = data.get("need_data_source_list", []) or []
    need_list = [k for k in need_list if k in DATA_SOURCES]
    if not need_list:
        need_list = list(DATA_SOURCES.keys())

    order_id = state.get("order_id") or new_order_id()

    summary = (
        f"工单解析完成：现象={_safe_get(parsed_ticket, 'phenomenon')}；"
        f"服务={_safe_get(parsed_ticket, 'service')}；"
        f"关键词={_safe_get(parsed_ticket, 'keywords')}；"
        f"工单编号={order_id}。"
        f"建议访问数据源：{[DATA_SOURCES[k]['label'] for k in need_list]}"
    )
    return {
        "parsed_ticket": parsed_ticket,
        "need_data_source_list": need_list,
        "order_id": order_id,
        "messages": [AIMessage(content=summary)],
    }


# ===========================================================================
# 节点 2：申请授权（人在回路 interrupt）
# ===========================================================================
def _build_authorize_prompt(need_list: list[str]) -> str:
    lines = ["根据当前故障信息，为完成完整排查，我需要访问以下本地资源，请确认授权："]
    for i, key in enumerate(need_list, start=1):
        lines.append(f"{i}. [ ] 读取{DATA_SOURCES[key]['label']}")
    lines.append("")
    lines.append("回复格式说明：")
    lines.append("- 回复【全部授权】：允许读取以上全部资源")
    lines.append("- 回复序号，例如「1、2、5」：只允许对应序号资源")
    lines.append("- 回复【全部拒绝】：不读取任何本地文件，仅基于已粘贴文本分析")
    return "\n".join(lines)


def _parse_authorization(user_input: str, need_list: list[str]) -> tuple[list[str], list[str]]:
    text = (user_input or "").strip()
    if re.search(r"全部授权|全部允许|^\s*all\s*$|授权全部", text, re.I):
        allow = list(need_list)
    elif re.search(r"全部拒绝|拒绝全部|全部不|^\s*none\s*$", text, re.I):
        allow = []
    else:
        nums = [int(n) for n in re.findall(r"\d+", text)]
        allow = [need_list[i - 1] for i in nums if 1 <= i <= len(need_list)]
    deny = [k for k in need_list if k not in allow]
    return allow, deny


def request_user_authorize_node(state: TicketState) -> dict:
    """输出需要授权的数据源，并 interrupt 暂停等待终端用户输入授权结果。"""
    from langgraph.types import interrupt

    need_list = state.get("need_data_source_list", [])
    prompt = _build_authorize_prompt(need_list)
    user_input: str = interrupt({"prompt": prompt, "need_list": need_list})

    allow, deny = _parse_authorization(user_input, need_list)
    msg = (
        f"授权结果：已授权 {[DATA_SOURCES[k]['label'] for k in allow]}；"
        f"已拒绝 {[DATA_SOURCES[k]['label'] for k in deny]}。"
    )
    return {
        "user_allow_list": allow,
        "user_deny_list": deny,
        "messages": [AIMessage(content=msg)],
    }


# ===========================================================================
# 节点 3：证据收集 Agent（仅对已授权数据源；知识库走 RAG）
# ===========================================================================
async def collect_evidence_node(state: TicketState) -> dict:
    """对 user_allow_list 内每个数据源收集证据。

    - knowledge_base：走 RAG 检索（两路召回 + RRF + cross-encoder），返回 Top5 片段；
    - 其余四类：调用 MCP 只读工具读取文件；
    - 被拒绝的数据源直接跳过；
    - 短期会话记忆缓存本轮 RAG 结果，避免重复检索。
    """
    allow_list = state.get("user_allow_list", [])
    deny_list = state.get("user_deny_list", [])
    evidence: dict[str, list[dict]] = {}
    session_memory: dict = dict(state.get("session_memory", {}))
    rag_results: list = list(state.get("rag_results", []))
    rag_low_score: bool = bool(state.get("rag_low_score", False))
    parsed = state.get("parsed_ticket", {}) or {}

    if not allow_list:
        return {
            "evidence": evidence,
            "session_memory": session_memory,
            "messages": [AIMessage(content="无任何数据源被授权，跳过证据收集。")],
        }

    # —— 知识库：RAG 检索（必须已授权）——
    if "knowledge_base" in allow_list:
        from .rag import build_rag_query, retrieve as rag_retrieve
        query = session_memory.get("rag_query") or build_rag_query(parsed) or state.get("user_ticket_input", "")
        rag_out = rag_retrieve(query)
        session_memory["rag_query"] = query
        session_memory["rag_result"] = rag_out
        rag_results = rag_out.get("chunks", [])
        rag_low_score = rag_out.get("low_score", True)
        evidence["knowledge_base"] = [
            {
                "file_path": c.get("source", ""),
                "content": c.get("text", ""),
                "score": c.get("score"),
                "section": c.get("section", ""),
            }
            for c in rag_results
        ]
        print(
            f"  [证据] 故障知识库（RAG）：召回 {len(rag_results)} 个片段，"
            f"best_score={rag_out.get('best_score')}，low_score={rag_low_score}",
            file=sys.stderr,
        )

    # —— 其余四类：MCP 只读 ——
    mcp_sources = [s for s in allow_list if s != "knowledge_base"]
    keywords = _safe_get(parsed, "keywords", default="")

    if mcp_sources:
        async with MCPFileClient() as mcp:
            for src in mcp_sources:
                base_rel = DATA_SOURCES[src]["base_dir"]
                base_abs = REPO_ROOT / base_rel
                label = DATA_SOURCES[src]["label"]

                listing = await mcp.call("glob_list", base_dir=str(base_abs), pattern="**/*")
                file_paths = [
                    REPO_ROOT / line.strip()
                    for line in listing.splitlines()
                    if line.strip() and not line.startswith("[")
                ][:MAX_FILES_PER_SOURCE]

                collected: list[dict] = []
                for fp in file_paths:
                    content = await mcp.call("read_file", file_path=str(fp))
                    collected.append({
                        "file_path": str(fp.resolve().relative_to(REPO_ROOT.resolve())),
                        "content": content,
                    })

                # 源码额外 grep 定位关键词行号
                if src == "code" and keywords and keywords != "用户未提供":
                    for fp in file_paths:
                        grep_res = await mcp.call(
                            "grep_search", pattern=re.escape(keywords), file_path=str(fp)
                        )
                        if not grep_res.startswith("[") and not grep_res.startswith("无匹配"):
                            rel = str(fp.resolve().relative_to(REPO_ROOT.resolve()))
                            for item in collected:
                                if item["file_path"] == rel:
                                    item["grep_hits"] = grep_res
                                    break

                evidence[src] = collected
                print(f"  [证据] {label}：读取 {len(collected)} 个文件", file=sys.stderr)

    denied_msg = (
        f"已跳过未授权数据源：{[DATA_SOURCES[k]['label'] for k in deny_list]}"
        if deny_list else ""
    )
    msg = (
        f"证据收集完成：文件 {sum(len(v) for k, v in evidence.items() if k != 'knowledge_base')} 个"
        f"+ 知识库 RAG 片段 {len(evidence.get('knowledge_base', []))} 个。"
    ) + denied_msg
    return {
        "evidence": evidence,
        "rag_results": rag_results,
        "rag_low_score": rag_low_score,
        "session_memory": session_memory,
        "messages": [AIMessage(content=msg)],
    }


# ===========================================================================
# 节点 4：故障诊断推理 Agent（含 RAG 约束 + 长期记忆参考）
# ===========================================================================
def _build_evidence_context(state: TicketState) -> str:
    """把 state 内全部证据拼成 LLM 可读的上下文文本。"""
    parts: list[str] = []
    parts.append("【用户原始工单输入】")
    parts.append(state.get("user_ticket_input", ""))
    parts.append("")

    parsed = state.get("parsed_ticket", {})
    parts.append("【工单解析结果】")
    parts.append(json.dumps(parsed, ensure_ascii=False, indent=2))
    parts.append("")

    evidence = state.get("evidence", {})
    if evidence:
        parts.append("【已授权读取的文件证据】")
        for src, files in evidence.items():
            label = DATA_SOURCES.get(src, {}).get("label", src)
            parts.append(f"--- 来源：{label} ---")
            for item in files:
                parts.append(f">>> 文件：{item['file_path']}"
                             + (f"（score={item.get('score')}）" if item.get("score") is not None else ""))
                content = item.get("content", "")
                parts.append(content[:4000])
                if item.get("grep_hits"):
                    parts.append(f"(grep 命中行：)\n{item['grep_hits']}")
                parts.append("")
    else:
        parts.append("【已授权读取的文件证据】无（用户未授权任何数据源）")

    # 知识库 RAG 低分提示
    if state.get("rag_low_score") and "knowledge_base" in (state.get("user_allow_list", [])):
        parts.append("【知识库检索提示】相关性分数过低：知识库无高相关性匹配案例，不得编造历史案例。")
        parts.append("")

    deny_list = state.get("user_deny_list", [])
    if deny_list:
        parts.append("【未授权、未参与分析的数据源】")
        parts.append("、".join(DATA_SOURCES[k]["label"] for k in deny_list))
        parts.append("")

    # 长期记忆参考（仅提示，禁止复用根因）
    try:
        from .persistence import load_long_term_memory
        history = load_long_term_memory(limit=5)
    except Exception:  # noqa: BLE001
        history = []
    if history:
        parts.append("【历史工单记忆（仅供参考，禁止直接复用历史根因结论）】")
        for h in history:
            parts.append(
                f"- {h.get('order_id','')} | {h.get('service','')} | "
                f"{h.get('phenomenon','')[:80]} | 置信度={h.get('confidence','')}"
            )
        parts.append("")

    return "\n".join(parts)


async def diagnosis_reason_node(state: TicketState) -> dict:
    """基于全部证据做根因推理，输出 diagnosis_result（根因/置信度/证据来源）。"""
    context = _build_evidence_context(state)
    llm = get_llm()
    resp = await llm.ainvoke([
        {"role": "system", "content": DIAGNOSIS_PROMPT},
        {"role": "user", "content": context},
    ])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)

    try:
        data = _extract_json(raw)
    except Exception:
        data = {
            "confidence": "低",
            "root_cause_analysis": "诊断模型输出解析失败，无法给出可靠结论，请补充更多故障信息或开放更多授权。",
            "reproduce_verification": "证据不足，建议补充故障信息后重新分析。",
            "fix_suggestions": "⚠️【需人工执行】请补充更多故障信息后再给出修复建议。",
            "prevention_suggestions": "证据不足，暂无法给出预防建议。",
            "evidence_sources": ["用户输入"],
        }

    data.setdefault("confidence", "低")
    for k in ("root_cause_analysis", "reproduce_verification",
              "fix_suggestions", "prevention_suggestions"):
        data.setdefault(k, "证据不足，暂无法给出结论。")
    data.setdefault("evidence_sources", ["用户输入"])
    data["fix_suggestions"] = _sanitize_fix(data["fix_suggestions"])

    msg = f"诊断推理完成，置信度：{data.get('confidence')}。"
    return {"diagnosis_result": data, "messages": [AIMessage(content=msg)]}


# ===========================================================================
# 修复建议兜底清洗
# ===========================================================================
_RUNNABLE_PREFIXES = (
    "rm ", "rm -", "mv ", "cp ", "chmod", "chown", "curl ", "wget ",
    "redis-cli", "mysql ", "psql ", "ssh ", "scp ", "kill ", "killall",
    "systemctl ", "service ", "docker ", "kubectl ", "git push",
    "sudo ", "DROP ", "DELETE ", "UPDATE ", "TRUNCATE ", "ALTER ",
)


def _sanitize_fix(text: str) -> str:
    """把疑似可直接运行的命令行改为自然语言意图描述（代码层二次保险）。"""
    if not text:
        return "⚠️【需人工执行】证据不足，暂无修复建议。"
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and any(stripped.startswith(p) or stripped.startswith("$ " + p) for p in _RUNNABLE_PREFIXES):
            out_lines.append(
                f"⚠️【需人工执行】请勿直接执行命令，改为描述操作意图："
                f"原疑似命令「{stripped}」请由运维人工评估后在受控环境执行。"
            )
        else:
            out_lines.append(line)
    joined = "\n".join(out_lines).strip()
    if "⚠️【需人工执行】" not in joined:
        joined = "⚠️【需人工执行】\n" + joined
    return joined


# ===========================================================================
# 节点 5：报告生成 Agent（固定 Markdown 模板 + 知识库引用来源）
# ===========================================================================
REPORT_TEMPLATE = """# 故障诊断报告

## 1. 故障概览
工单编号：{order_id}
故障现象：{phenomenon}
故障客户端：{client}
涉及服务：{service}
错误关键词 & 错误码：{keywords}
复现特征：{reproduce}

## 2. 排查信息源授权情况
✅ 已授权分析：
{authorized_summary}
❌ 未获得授权，未参与分析：
{denied_summary}

## 3. 收集证据汇总
> 用户原始输入证据：
{user_input_summary}

> 已授权读取的文件证据：
{evidence_summary}

> 知识库检索（RAG）匹配案例：
{rag_summary}

## 4. 疑似根因分析（置信度：{confidence}）
{root_cause_analysis}

## 5. 复现与验证建议
{reproduce_verification}

## 6. 修复操作建议
⚠️【需人工执行】
{fix_suggestions}

## 7. 预防优化建议
{prevention_suggestions}

## 8. 重要局限性声明
本智能体仅作为工程师辅助排查参考，不能替代人工运维；
部分信息源因为未授权没有参与分析，可能会遗漏故障点；证据不足请补充故障信息或者开放更多授权。
{unauth_note}
"""


def _format_evidence_summary(evidence: dict) -> str:
    """文件证据文本（知识库 RAG 单独展示，此处跳过 knowledge_base）。"""
    if not evidence:
        return "（无已授权读取的文件证据）"
    parts: list[str] = []
    for src, files in evidence.items():
        if src == "knowledge_base":
            continue  # RAG 单独成段
        label = DATA_SOURCES.get(src, {}).get("label", src)
        parts.append(f"【{label}】")
        for item in files:
            parts.append(f"  - {item['file_path']}")
            excerpt = item.get("content", "")[:1200]
            parts.append("    " + excerpt.replace("\n", "\n    "))
            if item.get("grep_hits"):
                parts.append("    (关键词命中行：)")
                parts.append("    " + str(item["grep_hits"]).replace("\n", "\n    "))
            parts.append("")
    return "\n".join(parts).strip() or "（无已授权读取的文件证据）"


def _format_rag_summary(state: TicketState) -> str:
    """知识库 RAG 匹配案例文本（含来源 + 相关性分数，用于报告溯源）。"""
    allow_list = state.get("user_allow_list", [])
    if "knowledge_base" not in allow_list:
        return "（知识库未获得授权，未执行 RAG 检索）"

    rag_low = state.get("rag_low_score", False)
    chunks = state.get("rag_results", []) or []
    if rag_low and not chunks:
        return "（检索相关性过低：知识库无高相关性匹配案例，未返回片段）"
    if not chunks:
        return "（RAG 检索未返回结果）"
    if rag_low:
        return (
            f"（检索相关性过低：best_score 过低，知识库无高相关性匹配案例；"
            f"以下为参考片段，请谨慎采纳）\n"
            + "\n".join(
                f"- {c.get('source','')}（score={c.get('score')}）："
                f"{(c.get('text','') or '')[:200].replace(chr(10),' ')}"
                for c in chunks
            )
        )
    return "\n".join(
        f"- {c.get('source','')}（score={c.get('score')}）："
        f"{(c.get('text','') or '')[:200].replace(chr(10),' ')}"
        for c in chunks
    )


def generate_report_node(state: TicketState) -> dict:
    """填充固定 Markdown 报告模板，生成 final_report_markdown。"""
    parsed = state.get("parsed_ticket", {}) or {}
    allow_list = state.get("user_allow_list", [])
    deny_list = state.get("user_deny_list", [])
    evidence = state.get("evidence", {}) or {}
    diag = state.get("diagnosis_result", {}) or {}

    authorized_summary = (
        "\n".join(f"- {DATA_SOURCES[k]['label']}" for k in allow_list)
        if allow_list else "（无）"
    )
    denied_summary = (
        "\n".join(f"- {DATA_SOURCES[k]['label']}" for k in deny_list)
        if deny_list else "无"
    )
    unauth_note = (
        f"本次因未授权未参与分析的数据源：{', '.join(DATA_SOURCES[k]['label'] for k in deny_list)}。"
        if deny_list else "本次所有申请的数据源均已授权。"
    )

    report = REPORT_TEMPLATE.format(
        order_id=state.get("order_id", "（未生成）"),
        phenomenon=_safe_get(parsed, "phenomenon"),
        client=_safe_get(parsed, "client"),
        service=_safe_get(parsed, "service"),
        keywords=_safe_get(parsed, "keywords"),
        reproduce=_safe_get(parsed, "reproduce"),
        authorized_summary=authorized_summary,
        denied_summary=denied_summary,
        user_input_summary=state.get("user_ticket_input", "（用户未提供）"),
        evidence_summary=_format_evidence_summary(evidence),
        rag_summary=_format_rag_summary(state),
        confidence=diag.get("confidence", "低"),
        root_cause_analysis=diag.get("root_cause_analysis", "证据不足，暂无法给出结论。"),
        reproduce_verification=diag.get("reproduce_verification", "证据不足，暂无法给出结论。"),
        fix_suggestions=diag.get("fix_suggestions", "⚠️【需人工执行】证据不足，暂无修复建议。"),
        prevention_suggestions=diag.get("prevention_suggestions", "证据不足，暂无法给出结论。"),
        unauth_note=unauth_note,
    )

    return {
        "final_report_markdown": report,
        "messages": [AIMessage(content="故障诊断报告已生成。")],
    }


# ===========================================================================
# 节点 6：工单持久化（序列化 state 关键内容 + 追加长期记忆）
# ===========================================================================
def persist_work_order_node(state: TicketState) -> dict:
    """把本次诊断 state 关键内容序列化为 JSON 落盘，返回 order_id。"""
    from .persistence import save_work_order
    order_id = save_work_order(state)
    return {
        "order_id": order_id,
        "messages": [AIMessage(content=f"工单已持久化：{order_id}")],
    }
