"""main.py
=========
SaaS 工单故障排查智能体 —— LangGraph CLI 命令行入口。

运行：
    python main.py                       # 进入交互循环（首次自动构建向量库）
    python main.py --rebuild-vector      # 强制重建知识库向量库后进入循环

交互流程
--------
1. 启动后打印横幅与配置自检结果；必要时构建 / 重建知识库向量库；
2. 进入交互循环，等待用户输入：
   - 输入 `/diagnose ...` 或故障描述：进入故障排查流程；
   - 输入普通文字：智能体做意图路由（可能反问确认意图）；
   - 输入 `sample` 载入内置示例工单；输入 `exit`/`quit` 退出；
3. 执行 graph：路由 → 工单解析 → 申请授权（interrupt）→ 证据收集（MCP+RAG）
   → 诊断推理 → 报告 → 持久化；中途的 interrupt 由 main.py 统一处理；
4. 打印最终 Markdown 故障报告与工单编号；
5. 回到步骤 2，每个工单独立 thread_id 隔离。

安全
----
- 所有 LLM 不绑定工具，无法自动调用 MCP；RAG 同属 knowledge_base 数据源，
  必须用户授权后才执行检索；
- 授权 / 意图确认指令只能由终端用户在 interrupt 暂停后输入。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.types import Command

# 确保能 import 同级 agent 包（直接 python main.py 运行时）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.graph import build_graph  # noqa: E402

# ---------------------------------------------------------------------------
# 内置示例工单：晚高峰下单 504（缓存击穿 -> Hikari 打满）场景
# ---------------------------------------------------------------------------
SAMPLE_TICKET = """【故障工单】晚高峰下单接口大面积 504
时间：2026-08-20 20:01 起，持续约 3-5 分钟
现象：租户 888、666 等大租户用户提交订单 POST /api/order/create 返回 504 Gateway Timeout（nginx）；
      20:05 后逐渐恢复，租户 999 等小租户基本正常。
前端报错：Chrome 控制台 POST https://app.example.com/api/order/create 504，x-trace-id=abc123def456
涉及服务：order-service
错误关键词：504 Gateway Timeout、HikariPool-1 Connection is not available、CannotGetJdbcConnectionException
复现特征：高峰期偶现、仅部分大租户
已做排查：网关与 order-service 日志已初步确认无发布变更。
"""


# ---------------------------------------------------------------------------
# Windows 控制台 UTF-8：避免中文 / emoji（⚠️）在 GBK 控制台乱码
# ---------------------------------------------------------------------------
def _enable_utf8_console() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 知识库向量库初始化（首次会下载嵌入模型；缺失依赖时优雅降级）
# ---------------------------------------------------------------------------
def _init_vector_store(rebuild: bool) -> None:
    try:
        from agent.rag import build_vector_store, vector_store_ready
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] RAG 模块不可用（{exc}）；知识库检索将降级跳过。")
        return
    try:
        if rebuild:
            print("[初始化] 重建知识库向量库（首次会下载嵌入模型，请稍候）...")
            n = build_vector_store(rebuild=True)
            print(f"[初始化] 向量库重建完成，共 {n} 个 chunk。")
        elif not vector_store_ready():
            print("[初始化] 未检测到向量库，自动构建知识库向量库（首次会下载嵌入模型，请稍候）...")
            n = build_vector_store(rebuild=False)
            print(f"[初始化] 向量库构建完成，共 {n} 个 chunk。")
        else:
            print("[初始化] 知识库向量库已就绪。")
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] 向量库构建失败（{exc}）；故障排查时知识库检索将降级跳过。")


# ---------------------------------------------------------------------------
# 配置自检
# ---------------------------------------------------------------------------
def _self_check(tenant_id: str = "") -> None:
    from agent.infra import infra_status, resolve_tenant

    print("=" * 64)
    print("SaaS 工单故障排查智能体 (LangGraph Demo)")
    print("=" * 64)
    api_key = os.getenv("API_KEY", "")
    base_url = os.getenv("BASE_URL", "")
    model = os.getenv("MODEL_NAME", "")
    print(f"  MODEL_NAME : {model or '(未设置)'}")
    print(f"  BASE_URL   : {base_url or '(未设置)'}")
    print(f"  API_KEY    : {'已设置' if api_key and not api_key.startswith('your_') else '⚠️ 未设置或仍为占位符'}")
    print(f"  TENANT_ID  : {resolve_tenant(tenant_id)}")
    st = infra_status()
    print(f"  基础设施   : Milvus={'✓' if st['milvus'] else '✗(RAG降级)'} "
          f"MySQL={'✓' if st['mysql'] else '✗(JSON降级)'} "
          f"Redis={'✓' if st['redis'] else '✗(进程内限流)'}")
    print("  数据源根目录：sample_data/、src/（仅只读，MCP 路径白盒校验）")
    print("  意图路由：`/diagnose` 前缀硬路由；其余由 LLM 识别（低置信度会反问）")
    print("  工程边界：LLM 限流/超时/重试/日志 + 节点级降级 + 租户隔离")
    print("  提示：输入 `sample` 载入示例工单；输入 `exit` 退出。")
    print("=" * 64)


def _read_ticket_input() -> str | None:
    """读取多行工单，以一个空行结束；返回 None 表示退出。"""
    print("\n请输入（多行，输入一个空行结束输入）：", flush=True)
    print("  · 输入 `/diagnose ...` 强制进入故障排查")
    print("  · 输入 `sample` 载入示例工单")
    print("  · 输入 `exit` 或 `quit` 退出")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        low = line.strip().lower()
        if low in ("exit", "quit"):
            return None
        if low == "sample" and not lines:
            return SAMPLE_TICKET
        if line.strip() == "" and lines:
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    return text or None


async def _await_input(prompt: str) -> str:
    """在子线程中读取终端输入，避免阻塞 asyncio 事件循环。"""
    return await asyncio.to_thread(input, prompt)


# ---------------------------------------------------------------------------
# 单次工单完整执行（含 interrupt 人在回路：统一处理路由反问 + 授权）
# ---------------------------------------------------------------------------
async def run_one_ticket(graph, ticket_text: str, tenant_id: str = "") -> None:
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    init_state = {
        "tenant_id": tenant_id,
        "user_ticket_input": ticket_text,
        "intent": "",
        "intent_confidence": 0.0,
        "parsed_ticket": {},
        "need_data_source_list": [],
        "order_id": "",
        "user_allow_list": [],
        "user_deny_list": [],
        "evidence": {},
        "rag_results": [],
        "rag_low_score": False,
        "session_memory": {},
        "long_term_memory": [],
        "diagnosis_result": {},
        "final_report_markdown": "",
        "degrade_notes": [],
        "call_count": 0,
        "messages": [HumanMessage(content=ticket_text)],
    }

    print("\n开始处理工单...")
    # 第一次推进：会跑到首个 interrupt（路由反问 / 授权）或直接跑完（闲聊）
    await graph.ainvoke(init_state, config)

    # 人在回路：统一处理任意 interrupt（路由低置信度反问、数据源授权申请）
    while True:
        snapshot = graph.get_state(config)
        pending = []
        for task in snapshot.tasks:
            pending.extend(task.interrupts or [])
        if not pending:
            if snapshot.next:
                await graph.ainvoke(None, config)
                continue
            break

        intr = pending[0]
        payload = intr.value if isinstance(intr.value, dict) else {"prompt": str(intr.value)}
        print("\n" + "-" * 64)
        print("[人在回路 · 等待你的输入]")
        print("-" * 64)
        print(payload.get("prompt", "请确认："))
        user_input = await _await_input("\n请输入: ")
        await graph.ainvoke(Command(resume=user_input), config)

    final = graph.get_state(config).values
    report = final.get("final_report_markdown", "（报告生成失败）")
    order_id = final.get("order_id", "")

    print("\n" + "=" * 64)
    print("【最终输出】")
    print("=" * 64)
    print(report)
    if order_id:
        print(f"\n工单已持久化，编号：{order_id}")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
async def main() -> None:
    _enable_utf8_console()
    load_dotenv()

    parser = argparse.ArgumentParser(description="SaaS 工单故障排查智能体 (LangGraph Demo)")
    parser.add_argument(
        "--rebuild-vector", action="store_true",
        help="强制重建知识库向量库（首次会下载嵌入模型）",
    )
    parser.add_argument(
        "--tenant", default=os.getenv("TENANT_ID", ""),
        help="租户标识（工程边界：所有查询强制带 tenant；默认读 TENANT_ID 环境变量）",
    )
    args = parser.parse_args()
    tenant = args.tenant or os.getenv("TENANT_ID", "")

    _init_vector_store(rebuild=args.rebuild_vector)
    _self_check(tenant_id=tenant)

    graph = build_graph()

    while True:
        ticket = _read_ticket_input()
        if ticket is None:
            print("已退出。")
            break
        try:
            await run_one_ticket(graph, ticket, tenant_id=tenant)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] 本轮执行失败：{exc}")
            print("请检查 .env 配置 / 网络 / 模型可用性后重试。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断，再见。")
