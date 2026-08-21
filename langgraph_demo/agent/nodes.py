"""agent/nodes.py
=================
LangGraph 5 个业务节点实现 + MCP 只读客户端 + LLM 工厂 + 人在回路授权。

节点清单
--------
1. parse_ticket_node            工单解析 Agent：解析工单 + 评估 need_data_source_list
2. request_user_authorize_node  申请授权（interrupt 暂停等终端输入）→ 填充 allow/deny
3. collect_evidence_node        仅对 allow_list 调用 MCP 只读工具收集证据
4. diagnosis_reason_node        综合证据做根因推理（含置信度）
5. generate_report_node         填充固定 Markdown 报告模板

安全要点
--------
- LLM 不绑定任何工具，无法自动调用 MCP；MCP 工具仅在 collect_evidence_node 内
  对「已授权」数据源显式调用，从机制上保证「先授权后读取」；
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

from .prompts import DIAGNOSIS_PROMPT, PARSE_TICKET_PROMPT
from .state import TicketState

# ---------------------------------------------------------------------------
# 路径常量
# 本文件位于 <repo_root>/langgraph_demo/agent/nodes.py
# 因此 langgraph_demo 目录 = 上两级缺失一节，实际为 parent.parent（agent -> langgraph_demo）
# repo_root = langgraph_demo 的上一级
# ---------------------------------------------------------------------------
LANGGRAPH_DEMO_DIR: Path = Path(__file__).resolve().parent.parent  # langgraph_demo/
REPO_ROOT: Path = LANGGRAPH_DEMO_DIR.parent                         # ticket-skill-agent/
MCP_SERVER_PATH: Path = LANGGRAPH_DEMO_DIR / "mcp_file_server.py"

# ---------------------------------------------------------------------------
# 五类数据源定义：key -> (中文标签, 相对仓库根的访问基准目录)
# 这 5 个 key 即 need_data_source_list / user_allow_list / user_deny_list 的取值
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
    # 延迟导入，避免在仅做语法检查等场景强依赖该库
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
    """从 LLM 输出中容错抽取 JSON 字典。

    依次尝试：```json 代码块 -> ``` 代码块 -> 首个 { 到末个 } 子串。
    """
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
    """通过 stdio 连接独立 FastMCP 只读文件服务的客户端。

    仅暴露 call() 方法调用 read_file / glob_list / grep_search 三个工具。
    用作 collect_evidence_node 内的证据读取后端，用完即关。
    """

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
# 节点 1：工单解析 Agent
# ===========================================================================
async def parse_ticket_node(state: TicketState) -> dict:
    """解析原始工单为结构化字典，并评估需要访问的数据源列表。"""
    ticket_text = state["user_ticket_input"]
    llm = get_llm()
    # 用 HumanMessage 携带工单文本，system prompt 已设定输出 JSON
    resp = await llm.ainvoke([
        {"role": "system", "content": PARSE_TICKET_PROMPT},
        {"role": "user", "content": ticket_text},
    ])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)

    try:
        data = _extract_json(raw)
    except Exception:
        # 解析失败时给出兜底，保证流程不中断
        data = {
            "parsed_ticket": {"phenomenon": ticket_text[:200]},
            "need_data_source_list": list(DATA_SOURCES.keys()),
        }

    parsed_ticket = data.get("parsed_ticket", {}) or {}
    need_list = data.get("need_data_source_list", []) or []
    # 清洗：只保留合法的数据源 key
    need_list = [k for k in need_list if k in DATA_SOURCES]
    if not need_list:
        # 兜底：全部 5 类
        need_list = list(DATA_SOURCES.keys())

    summary = (
        f"工单解析完成：现象={_safe_get(parsed_ticket, 'phenomenon')}；"
        f"服务={_safe_get(parsed_ticket, 'service')}；"
        f"关键词={_safe_get(parsed_ticket, 'keywords')}。"
        f"建议访问数据源：{[DATA_SOURCES[k]['label'] for k in need_list]}"
    )
    return {
        "parsed_ticket": parsed_ticket,
        "need_data_source_list": need_list,
        "messages": [AIMessage(content=summary)],
    }


# ===========================================================================
# 节点 2：申请授权（人在回路 interrupt）
# ===========================================================================
def _build_authorize_prompt(need_list: list[str]) -> str:
    """构造与 Skill 版本一致的授权申请话术。"""
    lines = [
        "根据当前故障信息，为完成完整排查，我需要访问以下本地资源，请确认授权："
    ]
    for i, key in enumerate(need_list, start=1):
        lines.append(f"{i}. [ ] 读取{DATA_SOURCES[key]['label']}")
    lines.append("")
    lines.append("回复格式说明：")
    lines.append("- 回复【全部授权】：允许读取以上全部资源")
    lines.append("- 回复序号，例如「1、2、5」：只允许对应序号资源")
    lines.append("- 回复【全部拒绝】：不读取任何本地文件，仅基于已粘贴文本分析")
    return "\n".join(lines)


def _parse_authorization(user_input: str, need_list: list[str]) -> tuple[list[str], list[str]]:
    """解析用户授权指令，返回 (allow_list, deny_list)。

    支持：
    - 全部授权 / all / 全部允许
    - 全部拒绝 / none / 拒绝全部
    - 序号：1、2、5 或 1 2 5 或 1,2,5
    """
    text = (user_input or "").strip()
    if re.search(r"全部授权|全部允许|^\s*all\s*$|授权全部", text, re.I):
        allow = list(need_list)
    elif re.search(r"全部拒绝|拒绝全部|全部不|^\s*none\s*$", text, re.I):
        allow = []
    else:
        nums = [int(n) for n in re.findall(r"\d+", text)]
        allow = [
            need_list[i - 1] for i in nums if 1 <= i <= len(need_list)
        ]
    deny = [k for k in need_list if k not in allow]
    return allow, deny


def request_user_authorize_node(state: TicketState) -> dict:
    """输出需要授权的数据源，并 interrupt 暂停等待终端用户输入授权结果。

    通过 langgraph interrupt() 暂停 graph：
    - 暂停时把授权申请话术 + need_list 经 interrupt value 暴露给调用方（main.py）；
    - main.py 读取终端输入后用 Command(resume=<用户输入>) 恢复；
    - 恢复后 interrupt() 返回用户输入字符串，本节点据此填充 allow/deny。
    """
    from langgraph.types import interrupt  # 延迟导入，避免对纯静态检查强依赖

    need_list = state.get("need_data_source_list", [])
    prompt = _build_authorize_prompt(need_list)

    # 暂停 graph，把话术交给 main.py 打印；恢复时返回用户输入字符串
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
# 节点 3：证据收集 Agent（仅对已授权数据源调用 MCP 只读工具）
# ===========================================================================
async def collect_evidence_node(state: TicketState) -> dict:
    """对 user_allow_list 内每个数据源，调用 MCP 只读工具读取文件证据。

    - 被拒绝的数据源直接跳过，不读取任何文件；
    - 每个数据源最多读 MAX_FILES_PER_SOURCE 个文件；
    - 证据存入 state["evidence"]，结构：{源key: [{"file_path", "content"}]}
    """
    allow_list = state.get("user_allow_list", [])
    deny_list = state.get("user_deny_list", [])
    evidence: dict[str, list[dict]] = {}

    if not allow_list:
        return {
            "evidence": evidence,
            "messages": [AIMessage(content="无任何数据源被授权，跳过证据收集。")],
        }

    # 用 grep 在源码中定位与故障相关的类/方法（仅当 code 被授权时）
    keywords = _safe_get(state.get("parsed_ticket", {}), "keywords", default="")
    service = _safe_get(state.get("parsed_ticket", {}), "service", default="")

    async with MCPFileClient() as mcp:
        for src in allow_list:
            base_rel = DATA_SOURCES[src]["base_dir"]
            base_abs = REPO_ROOT / base_rel
            label = DATA_SOURCES[src]["label"]

            # 列出该数据源下全部文件
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

            # 针对源码：额外用 grep 定位故障关键词所在行号（便于报告精确引用）
            if src == "code" and keywords and keywords != "用户未提供":
                for fp in file_paths:
                    grep_res = await mcp.call(
                        "grep_search",
                        pattern=re.escape(keywords),
                        file_path=str(fp),
                    )
                    if not grep_res.startswith("[") and not grep_res.startswith("无匹配"):
                        # 在对应文件证据里追加命中行信息
                        for item in collected:
                            if item["file_path"] == str(
                                fp.resolve().relative_to(REPO_ROOT.resolve())
                            ):
                                item["grep_hits"] = grep_res
                                break

            evidence[src] = collected
            print(f"  [证据] {label}：读取 {len(collected)} 个文件", file=sys.stderr)

    denied_msg = (
        f"已跳过未授权数据源：{[DATA_SOURCES[k]['label'] for k in deny_list]}"
        if deny_list else ""
    )
    msg = f"证据收集完成：已读取 {sum(len(v) for v in evidence.values())} 个文件。" + denied_msg
    return {"evidence": evidence, "messages": [AIMessage(content=msg)]}


# ===========================================================================
# 节点 4：故障诊断推理 Agent
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
                parts.append(f">>> 文件：{item['file_path']}")
                content = item.get("content", "")
                # 截断过长内容，避免上下文爆炸
                parts.append(content[:4000])
                if item.get("grep_hits"):
                    parts.append(f"(grep 命中行：)\n{item['grep_hits']}")
                parts.append("")
    else:
        parts.append("【已授权读取的文件证据】无（用户未授权任何数据源）")

    deny_list = state.get("user_deny_list", [])
    if deny_list:
        parts.append("")
        parts.append("【未授权、未参与分析的数据源】")
        parts.append("、".join(DATA_SOURCES[k]["label"] for k in deny_list))
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
        # 兜底：置信度低，提示证据不足
        data = {
            "confidence": "低",
            "root_cause_analysis": "诊断模型输出解析失败，无法给出可靠结论，请补充更多故障信息或开放更多授权。",
            "reproduce_verification": "证据不足，建议补充故障信息后重新分析。",
            "fix_suggestions": "⚠️【需人工执行】请补充更多故障信息后再给出修复建议。",
            "prevention_suggestions": "证据不足，暂无法给出预防建议。",
            "evidence_sources": ["用户输入"],
        }

    # 兜底补齐缺失字段
    data.setdefault("confidence", "低")
    for k in ("root_cause_analysis", "reproduce_verification",
              "fix_suggestions", "prevention_suggestions"):
        data.setdefault(k, "证据不足，暂无法给出结论。")
    data.setdefault("evidence_sources", ["用户输入"])

    # 清洗修复建议：移除可直接运行的命令，强制前置人工执行标识
    data["fix_suggestions"] = _sanitize_fix(data["fix_suggestions"])

    msg = f"诊断推理完成，置信度：{data.get('confidence')}。"
    return {"diagnosis_result": data, "messages": [AIMessage(content=msg)]}


# 直接可运行命令的兜底黑名单（仅清洗修复建议，不做任何执行）
_RUNNABLE_PREFIXES = (
    "rm ", "rm -", "mv ", "cp ", "chmod", "chown", "curl ", "wget ",
    "redis-cli", "mysql ", "psql ", "ssh ", "scp ", "kill ", "killall",
    "systemctl ", "service ", "docker ", "kubectl ", "git push",
    "sudo ", "DROP ", "DELETE ", "UPDATE ", "TRUNCATE ", "ALTER ",
)


def _sanitize_fix(text: str) -> str:
    """兜底清洗修复建议：把疑似可直接运行的命令行改为自然语言意图描述。

    这是对「prompt 禁止输出可运行命令」的代码层二次保险。
    """
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
    # 确保整体前置人工执行标识存在
    joined = "\n".join(out_lines).strip()
    if "⚠️【需人工执行】" not in joined:
        joined = "⚠️【需人工执行】\n" + joined
    return joined


# ===========================================================================
# 节点 5：报告生成 Agent（填充固定 Markdown 模板，与 Skill 版本完全一致）
# ===========================================================================
REPORT_TEMPLATE = """# 故障诊断报告

## 1. 故障概览
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
    """把证据字典格式化为报告第 3 节的文件证据文本。"""
    if not evidence:
        return "（无已授权读取的文件证据）"
    parts: list[str] = []
    for src, files in evidence.items():
        label = DATA_SOURCES.get(src, {}).get("label", src)
        parts.append(f"【{label}】")
        for item in files:
            parts.append(f"  - {item['file_path']}")
            content = item.get("content", "")
            excerpt = content[:1200]
            # 缩进展示，避免破坏 Markdown 结构
            parts.append("    " + excerpt.replace("\n", "\n    "))
            if item.get("grep_hits"):
                parts.append("    (关键词命中行：)")
                parts.append("    " + str(item["grep_hits"]).replace("\n", "\n    "))
            parts.append("")
    return "\n".join(parts).strip()


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
        phenomenon=_safe_get(parsed, "phenomenon"),
        client=_safe_get(parsed, "client"),
        service=_safe_get(parsed, "service"),
        keywords=_safe_get(parsed, "keywords"),
        reproduce=_safe_get(parsed, "reproduce"),
        authorized_summary=authorized_summary,
        denied_summary=denied_summary,
        user_input_summary=state.get("user_ticket_input", "（用户未提供）"),
        evidence_summary=_format_evidence_summary(evidence),
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
