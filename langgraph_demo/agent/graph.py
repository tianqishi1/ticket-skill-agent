"""agent/graph.py
=================
LangGraph StateGraph DAG 工作流组装（含意图路由 + 故障排查子图）。

DAG 拓扑：

    START
      │
      ▼
    router_node                       # 阶段0 意图路由（/diagnose 硬路由 或 LLM 识别）
      │  (条件路由)
      ├── troubleshoot ──────────────┐
      │                              ▼
      │   parse_ticket_node          # 阶段1 工单解析 + 评估数据源 + 生成 order_id
      │                              ▼
      │   request_user_authorize_node# 阶段2 申请授权（interrupt 暂停，等待终端用户输入）
      │                              ▼
      │   collect_evidence_node     # 阶段3 已授权数据源：MCP 只读 + 知识库 RAG 检索
      │                              ▼
      │   diagnosis_reason_node      # 阶段4 综合证据做根因推理（含置信度 + RAG 约束）
      │                              ▼
      │   generate_report_node      # 阶段5 填充固定 Markdown 报告模板
      │                              ▼
      │   persist_work_order_node    # 阶段6 序列化工单到 work_order_history/
      │                              ▼
      └──────────────────────────► END
      │
      └── chat ──► chat_respond_node ──► END   # 闲聊 / 代码问答，不读本地文件

说明
----
- 使用 StateGraph（显式节点 + 边），而非简单链式 LCEL 调用；
- 编译时挂载 MemorySaver checkpointer，配合 thread_id 隔离会话，
  支持 interrupt() 人在回路「暂停-恢复」；
- 两个 interrupt 点：router 低置信度反问、request_authorize 授权申请；
  main.py 的通用挂起检测循环可统一处理两者。
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    chat_respond_node,
    collect_evidence_node,
    diagnosis_reason_node,
    generate_report_node,
    parse_ticket_node,
    persist_work_order_node,
    request_user_authorize_node,
    route_after_router,
    router_node,
)
from .state import TicketState


def build_graph():
    """构建并编译工单故障排查 DAG 工作流，返回可执行的 CompiledGraph。"""
    workflow = StateGraph(TicketState)

    # 注册全部业务节点
    workflow.add_node("router", router_node)
    workflow.add_node("chat_respond", chat_respond_node)
    workflow.add_node("parse_ticket", parse_ticket_node)
    workflow.add_node("request_authorize", request_user_authorize_node)
    workflow.add_node("collect_evidence", collect_evidence_node)
    workflow.add_node("diagnosis_reason", diagnosis_reason_node)
    workflow.add_node("generate_report", generate_report_node)
    workflow.add_node("persist_work_order", persist_work_order_node)

    # 入口 → 路由
    workflow.add_edge(START, "router")

    # 路由条件分支：troubleshoot → 故障排查子图；其余 → 闲聊应答
    workflow.add_conditional_edges(
        "router",
        route_after_router,
        {"troubleshoot": "parse_ticket", "chat": "chat_respond"},
    )

    # 闲聊分支：直接结束（final_report_markdown 即回复）
    workflow.add_edge("chat_respond", END)

    # 故障排查子图：严格顺序，不可跳步
    workflow.add_edge("parse_ticket", "request_authorize")
    workflow.add_edge("request_authorize", "collect_evidence")
    workflow.add_edge("collect_evidence", "diagnosis_reason")
    workflow.add_edge("diagnosis_reason", "generate_report")
    workflow.add_edge("generate_report", "persist_work_order")
    workflow.add_edge("persist_work_order", END)

    # 挂载内存检查点：thread_id 隔离会话，支持 interrupt 暂停-恢复
    return workflow.compile(checkpointer=MemorySaver())
