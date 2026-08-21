"""agent/state.py
==================
LangGraph Graph State 状态结构体定义。

整个故障排查工作流的所有节点共享该 State；
每个节点读取所需字段、回写更新字段，由 LangGraph 在节点间自动传递与合并。

字段说明
--------
- user_ticket_input        : 用户原始工单输入（故障描述 / 报错 / 堆栈 / 接口报文）
- messages                 : LangGraph 标准消息列表（用 add_messages reducer 累加）

- intent                   : 路由节点输出意图 troubleshoot | chat | code
- intent_confidence        : 意图识别置信度 0~1（<0.7 会 interrupt 反问确认）

- parsed_ticket            : 工单解析节点输出的结构化字典（现象/客户端/服务/关键词/复现特征/已做排查）
- need_data_source_list    : 解析节点评估得到的「需要访问的 5 类数据源」键列表
- order_id                 : 工单唯一编号（持久化与历史记忆用）

- user_allow_list          : 用户在人在回路中授权允许访问的数据源键列表
- user_deny_list           : 用户拒绝访问的数据源键列表（被拒绝的直接跳过）

- evidence                 : 证据收集节点产出的证据字典，key=数据源键，value=证据列表
- rag_results              : RAG 检索 Top5 知识库片段（携带 source 元数据 + 相关性分数）
- rag_low_score            : 检索相关性过低标记（best_score < 阈值 → 知识库无高相关性匹配案例）

- session_memory           : 短期会话记忆：缓存本轮 RAG query/结果/证据，避免重复检索读取
- long_term_memory         : 历史工单摘要列表（仅作推理参考提示，禁止直接复用历史根因）

- diagnosis_result         : 诊断推理节点输出的根因/置信度/修复建议等结构化结果
- final_report_markdown    : 报告生成节点填充固定模板后的最终 Markdown 报告
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class TicketState(TypedDict):
    """工单故障排查 DAG 的共享状态结构体。"""

    # —— 输入 ——
    user_ticket_input: str
    messages: Annotated[list[BaseMessage], add_messages]

    # —— 路由阶段输出 ——
    intent: str
    intent_confidence: float

    # —— 工单解析阶段输出 ——
    parsed_ticket: dict
    need_data_source_list: list[str]
    order_id: str

    # —— 人在回路授权阶段输出 ——
    user_allow_list: list[str]
    user_deny_list: list[str]

    # —— 证据收集阶段输出 ——
    evidence: dict
    rag_results: list
    rag_low_score: bool

    # —— 记忆模块 ——
    session_memory: dict
    long_term_memory: list

    # —— 诊断推理 / 报告阶段输出 ——
    diagnosis_result: dict
    final_report_markdown: str
