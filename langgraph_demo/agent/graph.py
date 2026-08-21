"""agent/graph.py
=================
LangGraph StateGraph DAG 工作流组装。

DAG 拓扑（与原 Trae Skill 五阶段流程一一对应，严格不跳步）：

    START
      │
      ▼
    parse_ticket_node        # 阶段1 工单解析 + 评估需要的数据源
      │
      ▼
    request_user_authorize_node  # 阶段2 申请授权（interrupt 暂停，等待终端用户输入）
      │
      ▼
    collect_evidence_node    # 阶段3 仅对已授权数据源调用 MCP 只读工具收集证据
      │
      ▼
    diagnosis_reason_node    # 阶段4 综合证据做根因推理（含置信度）
      │
      ▼
    generate_report_node     # 阶段5 填充固定 Markdown 报告模板
      │
      ▼
    END

说明：
- 使用 StateGraph（显式节点 + 边），而非简单链式 LCEL 调用；
- 编译时挂载 MemorySaver checkpointer，配合 thread_id 隔离会话，
  支持 interrupt() 人在回路「暂停-恢复」；
- 阶段2 的暂停由 nodes.request_user_authorize_node 内部 interrupt() 触发，
  main.py 检测到挂起后读取终端输入并 Command(resume=...) 恢复。
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    collect_evidence_node,
    diagnosis_reason_node,
    generate_report_node,
    parse_ticket_node,
    request_user_authorize_node,
)
from .state import TicketState


def build_graph():
    """构建并编译工单故障排查 DAG 工作流，返回可执行的 CompiledGraph。"""
    workflow = StateGraph(TicketState)

    # 注册 5 个业务节点
    workflow.add_node("parse_ticket", parse_ticket_node)
    workflow.add_node("request_authorize", request_user_authorize_node)
    workflow.add_node("collect_evidence", collect_evidence_node)
    workflow.add_node("diagnosis_reason", diagnosis_reason_node)
    workflow.add_node("generate_report", generate_report_node)

    # 严格顺序连线（不可跳步）
    workflow.add_edge(START, "parse_ticket")
    workflow.add_edge("parse_ticket", "request_authorize")
    workflow.add_edge("request_authorize", "collect_evidence")
    workflow.add_edge("collect_evidence", "diagnosis_reason")
    workflow.add_edge("diagnosis_reason", "generate_report")
    workflow.add_edge("generate_report", END)

    # 挂载内存检查点：thread_id 隔离会话，支持 interrupt 暂停-恢复
    return workflow.compile(checkpointer=MemorySaver())
