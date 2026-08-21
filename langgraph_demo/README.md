# SaaS 工单故障排查智能体 —— LangGraph 独立可运行 Demo

脱离 Trae IDE 的独立 Python 工程，复用仓库根目录下的真实样本数据（`sample_data/`、`src/`），
基于 **LangGraph DAG 状态机 + FastMCP 只读文件服务 + 人在回路授权** 实现完整的 5 阶段故障排查流程。

> 本 Demo 是原 `skills/ticket-troubleshooting` Trae Skill 版本的独立工程化重实现，
> 报告模板与安全铁律与 Skill 版本完全一致。

## 核心特性

- **LangGraph DAG 状态机**：5 个业务节点显式连线，`MemorySaver` 做 checkpoint，`thread_id` 隔离会话，非简单链式调用。
- **人在回路授权（Human-in-the-Loop）**：基于 `langgraph.interrupt()` 暂停 graph，等待终端用户输入授权指令后才恢复；Agent 无法自动调用任何 MCP 工具。
- **独立 FastMCP 只读文件服务（代码层硬限制）**：仅暴露 `read_file` / `glob_list` / `grep_search` 三个只读工具；代码层面禁止写文件、删除、执行 shell、网络请求；路径 `resolve()` 后白盒校验，仅允许 `sample_data/` 与 `src/`，越界直接拒绝。
- **OpenAI 兼容 LLM**：通过 `.env` 适配豆包 Seed API（Ark 网关）。
- **固定 Markdown 报告模板**：修复建议全部标记 `⚠️【需人工执行】`，禁止输出可直接运行的 shell / sql 命令。

## 目录结构

```
langgraph_demo/
├─ .env.example          # 环境变量样例（仅占位符）
├─ pyproject.toml        # 工程元信息（依赖见 requirements.txt）
├─ requirements.txt      # 依赖清单
├─ mcp_file_server.py    # FastMCP 只读文件服务（代码层安全限制）
├─ agent/
│   ├─ __init__.py
│   ├─ state.py          # Graph State 状态结构体
│   ├─ nodes.py          # 5 个业务节点 + MCP 只读客户端 + LLM 工厂
│   ├─ graph.py          # StateGraph DAG 组装 + MemorySaver
│   └─ prompts.py        # 各子 Agent 系统提示词常量
└─ main.py               # CLI 入口，交互循环，处理授权输入

（上级目录，已存在，复用，不重复生成）
sample_data/  # frontend_samples / sample_tickets / monitor_samples / knowledge_base
src/          # order-service 模拟业务源码
```

## 工作流

```
START → parse_ticket → request_authorize(interrupt 暂停) → collect_evidence → diagnosis_reason → generate_report → END
```

1. `parse_ticket`：解析工单 → 结构化字段 + 评估需要访问的 5 类数据源
2. `request_authorize`：输出授权申请话术 → **interrupt 暂停**，等待终端用户输入（全部授权 / 序号 / 全部拒绝）→ 填充 allow/deny
3. `collect_evidence`：仅对已授权数据源调用 MCP 只读工具读取文件证据（拒绝的直接跳过）
4. `diagnosis_reason`：综合全部证据做根因推理，输出根因 + 置信度 + 证据来源（证据不足时降低置信度，不编造）
5. `generate_report`：填充固定 Markdown 模板，输出最终报告

## 安装依赖

> 需要 Python ≥ 3.10。

```bash
cd langgraph_demo
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 配置 .env

```bash
cp .env.example .env
```

编辑 `.env`，填写豆包 API 信息（**禁止硬编码到代码**）：

```
API_KEY=你的豆包API Key
BASE_URL=https://ark.cn-beijing.volces.com/api/v3
MODEL_NAME=doubao-seed-1-6-250715
LLM_TIMEOUT=60
```

## 运行 Demo

```bash
python main.py
```

启动后进入交互循环：

1. 直接输入 `sample` 回车 → 载入内置示例工单（晚高峰下单 504 场景）；
2. Agent 解析工单后会在「申请授权」处暂停，打印需要访问的数据源清单；
3. 按提示输入授权指令，例如：
   - `全部授权` —— 允许读取全部申请的数据源
   - `1、2、5` —— 仅授权第 1、2、5 项
   - `全部拒绝` —— 不读取任何本地文件，仅基于文本分析
4. Agent 收集证据 → 推理根因 → 输出最终 Markdown 故障报告；
5. 完成一轮后回到输入提示，可继续输入下一个工单；输入 `exit` 退出。

## 安全说明

- MCP 文件服务的安全完全由**代码层硬限制**实现，不依赖大模型 prompt：
  - 三个工具函数体内只调用只读 API（`read_bytes` / `read_text` / `Path.glob` / `re`）；
  - `_resolve_allowed()` 对所有传入路径 `resolve()` 后强制白盒比对，越界抛 `PermissionError`；
  - 允许目录仅 `sample_data/` 与 `src/`，无法向上跳出项目根目录。
- Agent 侧：LLM 不绑定任何工具，仅产出文本 / JSON；MCP 工具只能在 `collect_evidence_node` 内对**已授权**数据源显式调用，机制上保证「先授权后读取」。

## 局限性

- MVP Demo，未接入 RAG 向量库；知识库检索直接用 MCP `read_file` / `grep_search` 读取 `sample_data/knowledge_base` 下的 Markdown。
- 仅 CLI，无 Web 界面。
- 当证据不足，诊断 Agent 会降低置信度并提示证据不足，不编造根因。
