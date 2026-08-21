"""main.py
=========
SaaS 工单故障排查智能体 —— LangGraph CLI 命令行入口。

运行：python main.py

交互流程
--------
1. 启动后打印横幅与配置自检结果；
2. 进入交互循环，等待用户输入故障工单（多行，以一个空行结束输入）；
   - 输入 `sample` 直接载入内置示例工单（晚高峰下单 504 场景）；
   - 输入 `exit` 或 `quit` 退出；
3. 执行 graph：
   a. 工单解析 -> 评估数据源 -> 申请授权（graph 在此 interrupt 暂停）；
   b. main.py 检测到挂起后打印授权申请，读取终端授权指令；
   c. 用 Command(resume=<授权指令>) 恢复 graph -> 证据收集 -> 诊断 -> 报告；
4. 打印最终 Markdown 故障报告；
5. 回到步骤 2，等待下一个工单（每个工单独立 thread_id 隔离）。

安全
----
- 所有 LLM 不绑定工具，无法自动调用 MCP；
- 授权指令只能由终端用户在 interrupt 暂停后输入，机制上保证「先授权后读取」。
"""
from __future__ import annotations

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
# 内置示例工单：晚高峰下单 504（缓存击穿 -> Hikari 打满）场景，便于一键演示
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
# 配置自检
# ---------------------------------------------------------------------------
def _self_check() -> None:
    print("=" * 64)
    print("SaaS 工单故障排查智能体 (LangGraph Demo)")
    print("=" * 64)
    api_key = os.getenv("API_KEY", "")
    base_url = os.getenv("BASE_URL", "")
    model = os.getenv("MODEL_NAME", "")
    print(f"  MODEL_NAME : {model or '(未设置)'}")
    print(f"  BASE_URL   : {base_url or '(未设置)'}")
    print(f"  API_KEY    : {'已设置' if api_key and not api_key.startswith('your_') else '⚠️ 未设置或仍为占位符'}")
    print("  数据源根目录：sample_data/、src/（仅只读）")
    print("  提示：输入 `sample` 载入示例工单；输入 `exit` 退出。")
    print("=" * 64)


def _read_ticket_input() -> str | None:
    """读取多行工单，以一个空行结束；返回 None 表示退出。"""
    print("\n请输入故障工单（多行，输入一个空行结束输入）：", flush=True)
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
# 单次工单完整执行（含 interrupt 人在回路）
# ---------------------------------------------------------------------------
async def run_one_ticket(graph, ticket_text: str) -> None:
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    init_state = {
        "user_ticket_input": ticket_text,
        "parsed_ticket": {},
        "need_data_source_list": [],
        "user_allow_list": [],
        "user_deny_list": [],
        "evidence": {},
        "diagnosis_result": {},
        "final_report_markdown": "",
        "messages": [HumanMessage(content=ticket_text)],
    }

    print("\n[1/5] 工单解析中...")
    # 第一次推进：会跑到 request_authorize 节点处 interrupt 暂停
    await graph.ainvoke(init_state, config)

    # 人在回路：处理授权 interrupt
    while True:
        snapshot = graph.get_state(config)
        pending = []
        for task in snapshot.tasks:
            pending.extend(task.interrupts or [])
        if not pending:
            # 无挂起中断：若仍有未执行节点则继续推进，否则结束
            if snapshot.next:
                await graph.ainvoke(None, config)
                continue
            break

        intr = pending[0]
        payload = intr.value if isinstance(intr.value, dict) else {"prompt": str(intr.value)}
        # 打印授权申请话术
        print("\n" + "=" * 64)
        print("[2/5] 申请数据源授权（人在回路）")
        print("=" * 64)
        print(payload.get("prompt", "请确认授权："))
        user_input = await _await_input(
            "\n请输入授权指令（例如「全部授权」/「1、2、5」/「全部拒绝」）: "
        )
        # 用用户输入恢复 graph：interrupt() 会返回该字符串
        await graph.ainvoke(Command(resume=user_input), config)

    print("\n[3-5/5] 证据收集 -> 诊断推理 -> 报告生成完成。")
    final = graph.get_state(config).values
    report = final.get("final_report_markdown", "（报告生成失败）")

    print("\n" + "=" * 64)
    print("【最终故障诊断报告】")
    print("=" * 64)
    print(report)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
async def main() -> None:
    _enable_utf8_console()  # Windows 控制台 UTF-8，避免中文乱码
    load_dotenv()  # 加载 .env
    _self_check()

    graph = build_graph()

    while True:
        ticket = _read_ticket_input()
        if ticket is None:
            print("已退出。")
            break
        try:
            await run_one_ticket(graph, ticket)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] 本轮工单执行失败：{exc}")
            print("请检查 .env 配置 / 网络 / 模型可用性后重试。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断，再见。")
