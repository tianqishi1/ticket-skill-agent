"""agent 包 —— LangGraph 工单故障排查智能体业务模块。

子模块：
- state.py    : Graph State 状态结构体定义
- prompts.py  : 各子 Agent 系统提示词常量
- nodes.py    : 5 个业务节点 + MCP 只读客户端 + LLM 工厂
- graph.py    : StateGraph DAG 组装 + MemorySaver checkpoint
"""
