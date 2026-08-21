"""agent/state.py
==================
LangGraph Graph State 状态结构体定义。

整个故障排查工作流的所有节点共享该 State；
每个节点读取所需字段、回写更新字段，由 LangGraph 在节点间自动传递与合并。

字段说明
--------
- user_ticket_input        : 用户原始工单输入（故障描述 / 报错 / 堆栈 / 接口报文）
- parsed_ticket            : 工单解析节点输出的结构化字典（现象/客户端/服务/关键词/复现特征/已做排查）
- need_data_source_list    : 解析节点评估得到的「需要访问的 5 类数据源」键列表
- user_allow_list          : 用户在人在回路中授权允许访问的数据源键列表
- user_deny_list           : 用户拒绝访问的数据源键列表（被拒绝的直接跳过）
- evidence                 : 证据收集节点产出的证据字典，key=数据源键，value=文件证据列表
- diagnosis_result         : 诊断推理节点输出的根因/置信度/修复建议等结构化结果
- final_report_markdown    : 报告生成节点填充固定模板后的最终 Markdown 报告
- messages                 : LangGraph 标准消息列表（用 add_messages reducer 累加）
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class TicketState(TypedDict):
    """工单故障排查 DAG 的共享状态结构体。"""

    # —— 输入 ——
    user_ticket_input: str

    # —— 工单解析阶段输出 ——
    parsed_ticket: dict
    need_data_source_list: list[str]

    # —— 人在回路授权阶段输出 ——
    user_allow_list: list[str]
    user_deny_list: list[str]

    # —— 证据收集阶段输出 ——
    evidence: dict

    # —— 诊断推理阶段输出 ——
    diagnosis_result: dict

    # —— 报告生成阶段输出 ——
    final_report_markdown: str

    # —— LangGraph 标准消息列表（跨节点累加） ——
    messages: Annotated[list[BaseMessage], add_messages]
