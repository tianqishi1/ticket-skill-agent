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
6. generate_report_node        填充固定 Markdown 报告模板（含知识库引用来源 + 租户 + 降级说明）
7. persist_work_order_node     序列化 state 关键内容（MySQL 优先，失败降级 JSON）

工程边界（对应「补齐工程边界」需求）
---------------------------------
- 租户隔离：所有外部查询（MCP / RAG / 持久化）强制带 tenant_id（infra.resolve_tenant）；
- 超时 + 重试：LLM 走 _invoke_llm（限流 → with_retry 超时退避 → 日志），
  MCP 走 MCPFileClient.call（超时 + 重试 + 日志）；
- 限流：infra.llm_rate_allowed 按 tenant 维度计数（Redis 优先，进程内退化）；
- 异常降级：每个节点 try/except 包裹，失败写 degrade_notes 并返回降级结果，绝不抛堆栈中断主流程；
- 调用日志：infra.log_call 记录 prompt / 工具入参出参 / token 消耗，写 logs/calls.jsonl。

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

from .infra import (
    extract_tokens,
    llm_rate_allowed,
    log_call,
    resolve_tenant,
    with_retry,
)
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
    """根据 .env 配置返回 ChatOpenAI 实例（含超时 + max_retries）。"""
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
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
    )


def _messages_to_text(messages) -> str:
    """把消息列表渲染成可记入日志的文本（role + content）。"""
    parts: list[str] = []
    for m in messages:
        if isinstance(m, dict):
            parts.append(f"[{m.get('role', '?')}] {m.get('content', '')}")
        else:
            role = getattr(m, "type", getattr(m, "role", "?"))
            parts.append(f"[{role}] {getattr(m, 'content', '')}")
    return "\n".join(parts)


async def _invoke_llm(messages, *, tenant_id: str, component: str):
    """统一 LLM 调用入口：限流 → 超时+重试 → 调用日志。失败抛异常由节点降级捕获。

    工程边界闭环：限流防打爆、超时重试防抖动、日志留痕可复盘。
    """
    ok, reason = llm_rate_allowed(tenant_id)
    if not ok:
        log_call(component, tenant_id=tenant_id, error=reason)
        raise RuntimeError(reason)
    llm = get_llm()
    timeout = int(os.getenv("LLM_TIMEOUT", "60"))
    prompt_text = _messages_to_text(messages)

    async def _do():
        return await llm.ainvoke(messages)

    try:
        resp = await with_retry(
            _do,
            retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            base_delay=1.5,
            timeout=timeout,
            label=component,
        )
    except Exception as exc:  # noqa: BLE001
        log_call(component, tenant_id=tenant_id, prompt=prompt_text, error=str(exc))
        raise
    tokens = extract_tokens(resp)
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    log_call(component, tenant_id=tenant_id, prompt=prompt_text,
             output=content, tokens=tokens)
    return resp


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
    """通过 stdio 连接独立 FastMCP 只读文件服务的客户端（带超时/重试/日志）。"""

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

    async def call(self, tool_name: str, *, tenant_id: str = "", **kwargs) -> str:
        """调用一个 MCP 只读工具（超时 + 重试 + 日志），返回拼接后的文本结果。"""
        label = f"mcp:{tool_name}"
        timeout = float(os.getenv("MCP_TIMEOUT", "30"))

        async def _do():
            return await self._session.call_tool(tool_name, kwargs)

        try:
            result = await with_retry(
                _do, retries=1, base_delay=1.0, timeout=timeout, label=label
            )
        except Exception as exc:  # noqa: BLE001
            log_call(label, tenant_id=tenant_id, input_params=kwargs, error=str(exc))
            raise
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        out = "\n".join(parts)
        log_call(label, tenant_id=tenant_id, input_params=kwargs, output=out)
        return out

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

    tenant = resolve_tenant(state.get("tenant_id"))
    text = state.get("user_ticket_input", "")
    stripped = text.lstrip()
    notes = list(state.get("degrade_notes", []))
    call_count = int(state.get("call_count", 0))

    if stripped.startswith("/diagnose"):
        # 代码层硬路由：直接进入故障排查子图，不经过 LLM
        intent, conf, reason = "troubleshoot", 1.0, "命中 /diagnose 前缀，代码硬路由"
    else:
        try:
            resp = await _invoke_llm(
                [
                    {"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": text[:2000]},
                ],
                tenant_id=tenant,
                component="router",
            )
            call_count += 1
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            try:
                data = _extract_json(raw)
            except Exception:  # noqa: BLE001
                data = {}
            intent = str(data.get("intent", "chat")).lower()
            if intent not in ("troubleshoot", "chat", "code"):
                intent = "chat"
            try:
                conf = float(data.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            reason = str(data.get("reason", ""))
        except Exception as exc:  # noqa: BLE001  LLM 失败降级
            notes.append(f"路由 LLM 失败，降级为闲聊：{exc}")
            intent, conf, reason = "chat", 0.5, f"LLM 路由失败降级：{exc}"

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
    return {
        "intent": intent,
        "intent_confidence": conf,
        "degrade_notes": notes,
        "call_count": call_count,
        "messages": [AIMessage(content=msg)],
    }


def route_after_router(state: TicketState) -> str:
    """条件路由：troubleshoot → 故障排查子图；其余 → 闲聊应答。"""
    return "troubleshoot" if state.get("intent") == "troubleshoot" else "chat"


# ===========================================================================
# 节点 chat：闲聊 / 代码问答（不读本地文件、不调用任何工具）
# ===========================================================================
async def chat_respond_node(state: TicketState) -> dict:
    tenant = resolve_tenant(state.get("tenant_id"))
    notes = list(state.get("degrade_notes", []))
    text = state.get("user_ticket_input", "")
    try:
        resp = await _invoke_llm(
            [
                {"role": "system", "content": CHAT_PROMPT},
                {"role": "user", "content": text[:4000]},
            ],
            tenant_id=tenant,
            component="chat",
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as exc:  # noqa: BLE001
        content = (
            f"（LLM 不可用，降级回复）我暂时无法响应：{exc}\n"
            "如需排查故障，请用 `/diagnose` 开头描述故障现象。"
        )
        notes.append(f"闲聊 LLM 失败降级：{exc}")
    return {
        "final_report_markdown": content,
        "degrade_notes": notes,
        "call_count": int(state.get("call_count", 0)) + 1,
        "messages": [AIMessage(content="闲聊 / 代码问答已回复。")],
    }


# ===========================================================================
# 节点 1：工单解析 Agent
# ===========================================================================
async def parse_ticket_node(state: TicketState) -> dict:
    """解析原始工单为结构化字典，评估需要的数据源列表，并生成 order_id。"""
    from .persistence import new_order_id

    tenant = resolve_tenant(state.get("tenant_id"))
    notes = list(state.get("degrade_notes", []))
    ticket_text = state.get("user_ticket_input", "")

    try:
        resp = await _invoke_llm(
            [
                {"role": "system", "content": PARSE_TICKET_PROMPT},
                {"role": "user", "content": ticket_text},
            ],
            tenant_id=tenant,
            component="parse_ticket",
        )
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        call_inc = 1
        try:
            data = _extract_json(raw)
        except Exception:  # noqa: BLE001  LLM 输出解析失败 → 兜底结构
            notes.append("工单解析 LLM 输出非 JSON，使用兜底结构化结果。")
            data = {
                "parsed_ticket": {"phenomenon": ticket_text[:200]},
                "need_data_source_list": list(DATA_SOURCES.keys()),
            }
    except Exception as exc:  # noqa: BLE001  LLM 调用失败 → 兜底
        notes.append(f"工单解析 LLM 调用失败，使用兜底结果：{exc}")
        data = {
            "parsed_ticket": {"phenomenon": ticket_text[:200]},
            "need_data_source_list": list(DATA_SOURCES.keys()),
        }
        call_inc = 0

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
        "degrade_notes": notes,
        "call_count": int(state.get("call_count", 0)) + call_inc,
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
# 节点 3：证据收集 Agent（仅对已授权数据源；知识库走 RAG；按源降级）
# ===========================================================================
async def collect_evidence_node(state: TicketState) -> dict:
    """对 user_allow_list 内每个数据源收集证据。

    - knowledge_base：走 RAG 检索（两路召回 + RRF + cross-encoder），返回 Top5 片段；
    - 其余四类：调用 MCP 只读工具读取文件；
    - 被拒绝的数据源直接跳过；
    - 单源失败仅降级该源（写 degrade_notes），不影响其他源；
    - 短期会话记忆缓存本轮 RAG 结果，避免重复检索。
    """
    tenant = resolve_tenant(state.get("tenant_id"))
    allow_list = state.get("user_allow_list", [])
    deny_list = state.get("user_deny_list", [])
    evidence: dict[str, list[dict]] = {}
    session_memory: dict = dict(state.get("session_memory", {}))
    rag_results: list = list(state.get("rag_results", []))
    rag_low_score: bool = bool(state.get("rag_low_score", False))
    parsed = state.get("parsed_ticket", {}) or {}
    notes = list(state.get("degrade_notes", []))

    if not allow_list:
        return {
            "evidence": evidence,
            "session_memory": session_memory,
            "degrade_notes": notes,
            "messages": [AIMessage(content="无任何数据源被授权，跳过证据收集。")],
        }

    # —— 知识库：RAG 检索（必须已授权）——
    if "knowledge_base" in allow_list:
        from .rag import build_rag_query, retrieve as rag_retrieve
        query = session_memory.get("rag_query") or build_rag_query(parsed) or state.get("user_ticket_input", "")
        try:
            rag_out = rag_retrieve(query, tenant_id=tenant)
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
            if rag_out.get("reason"):
                notes.append(f"知识库 RAG 降级：{rag_out.get('reason')}")
        except Exception as exc:  # noqa: BLE001  RAG 整体失败降级
            rag_low_score = True
            notes.append(f"知识库 RAG 检索失败，已跳过：{exc}")
            print(f"  [证据] 知识库 RAG 失败：{exc}", file=sys.stderr)

    # —— 其余四类：MCP 只读（按源降级）——
    mcp_sources = [s for s in allow_list if s != "knowledge_base"]
    keywords = _safe_get(parsed, "keywords", default="")

    if mcp_sources:
        try:
            async with MCPFileClient() as mcp:
                for src in mcp_sources:
                    base_rel = DATA_SOURCES[src]["base_dir"]
                    base_abs = REPO_ROOT / base_rel
                    label = DATA_SOURCES[src]["label"]
                    try:
                        listing = await mcp.call(
                            "glob_list", tenant_id=tenant,
                            base_dir=str(base_abs), pattern="**/*",
                        )
                        file_paths = [
                            REPO_ROOT / line.strip()
                            for line in listing.splitlines()
                            if line.strip() and not line.startswith("[")
                        ][:MAX_FILES_PER_SOURCE]

                        collected: list[dict] = []
                        for fp in file_paths:
                            content = await mcp.call(
                                "read_file", tenant_id=tenant, file_path=str(fp),
                            )
                            collected.append({
                                "file_path": str(fp.resolve().relative_to(REPO_ROOT.resolve())),
                                "content": content,
                            })

                        # 源码额外 grep 定位关键词行号
                        if src == "code" and keywords and keywords != "用户未提供":
                            for fp in file_paths:
                                grep_res = await mcp.call(
                                    "grep_search", tenant_id=tenant,
                                    pattern=re.escape(keywords), file_path=str(fp),
                                )
                                if not grep_res.startswith("[") and not grep_res.startswith("无匹配"):
                                    rel = str(fp.resolve().relative_to(REPO_ROOT.resolve()))
                                    for item in collected:
                                        if item["file_path"] == rel:
                                            item["grep_hits"] = grep_res
                                            break

                        evidence[src] = collected
                        print(f"  [证据] {label}：读取 {len(collected)} 个文件", file=sys.stderr)
                    except Exception as exc:  # noqa: BLE001  单源失败降级
                        notes.append(f"{label} 证据收集失败，已跳过：{exc}")
                        print(f"  [证据] {label} 失败：{exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001  MCP 子进程起不来
            notes.append(f"MCP 文件服务不可用，全部本地源降级跳过：{exc}")
            print(f"  [证据] MCP 不可用：{exc}", file=sys.stderr)

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
        "degrade_notes": notes,
        "messages": [AIMessage(content=msg)],
    }


# ===========================================================================
# 节点 4：故障诊断推理 Agent（含 RAG 约束 + 长期记忆参考）
# ===========================================================================
def _build_evidence_context(state: TicketState) -> str:
    """把 state 内全部证据拼成 LLM 可读的上下文文本（含租户 + 长期记忆）。"""
    tenant = resolve_tenant(state.get("tenant_id"))
    parts: list[str] = []
    parts.append(f"【租户】{tenant}")
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

    # 长期记忆参考（仅提示，禁止复用根因）——带租户隔离
    try:
        from .persistence import load_long_term_memory
        history = load_long_term_memory(tenant_id=tenant, limit=5)
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
    tenant = resolve_tenant(state.get("tenant_id"))
    notes = list(state.get("degrade_notes", []))
    context = _build_evidence_context(state)
    call_inc = 0
    try:
        resp = await _invoke_llm(
            [
                {"role": "system", "content": DIAGNOSIS_PROMPT},
                {"role": "user", "content": context},
            ],
            tenant_id=tenant,
            component="diagnosis",
        )
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        call_inc = 1
        try:
            data = _extract_json(raw)
        except Exception:  # noqa: BLE001
            notes.append("诊断 LLM 输出非 JSON，使用降级结论。")
            data = None
    except Exception as exc:  # noqa: BLE001  LLM 调用失败 → 降级低置信结论
        notes.append(f"诊断 LLM 调用失败，降级低置信结论：{exc}")
        data = None

    if not data:
        data = {
            "confidence": "低",
            "root_cause_analysis": "诊断模型不可用或输出解析失败，无法给出可靠结论，请补充更多故障信息或开放更多授权。",
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
    return {
        "diagnosis_result": data,
        "degrade_notes": notes,
        "call_count": int(state.get("call_count", 0)) + call_inc,
        "messages": [AIMessage(content=msg)],
    }


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
# 节点 5：报告生成 Agent（固定 Markdown 模板 + 知识库引用来源 + 租户 + 降级）
# ===========================================================================
REPORT_TEMPLATE = """# 故障诊断报告

## 1. 故障概览
工单编号：{order_id}
租户：{tenant_id}
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
{degrade_block}
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


def _format_degrade_block(notes: list) -> str:
    """把降级记录渲染成报告段落（无降级则空串，保持报告整洁）。"""
    if not notes:
        return ""
    lines = ["", "## 9. 工程边界降级说明", "本轮执行中发生以下降级（不影响主流程，已自动兜底）："]
    for n in notes:
        lines.append(f"- {n}")
    return "\n".join(lines)


def generate_report_node(state: TicketState) -> dict:
    """填充固定 Markdown 报告模板，生成 final_report_markdown。"""
    parsed = state.get("parsed_ticket", {}) or {}
    allow_list = state.get("user_allow_list", [])
    deny_list = state.get("user_deny_list", [])
    evidence = state.get("evidence", {}) or {}
    diag = state.get("diagnosis_result", {}) or {}
    notes = list(state.get("degrade_notes", []))

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
        tenant_id=resolve_tenant(state.get("tenant_id")),
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
        degrade_block=_format_degrade_block(notes),
    )

    return {
        "final_report_markdown": report,
        "messages": [AIMessage(content="故障诊断报告已生成。")],
    }


# ===========================================================================
# 节点 6：工单持久化（MySQL 优先，失败降级 JSON）
# ===========================================================================
def persist_work_order_node(state: TicketState) -> dict:
    """把本次诊断 state 关键内容落库（MySQL 优先，失败降级 JSON），返回 order_id。"""
    from .persistence import save_work_order
    try:
        order_id = save_work_order(state)
        msg = f"工单已持久化：{order_id}"
    except Exception as exc:  # noqa: BLE001  持久化失败不阻断报告已生成
        order_id = state.get("order_id", "（未生成）")
        msg = f"工单持久化失败（已降级）：{exc}"
        log_call("persistence:save", tenant_id=resolve_tenant(state.get("tenant_id")),
                 input_params={"order_id": order_id}, error=str(exc))
    return {
        "order_id": order_id,
        "messages": [AIMessage(content=msg)],
    }
