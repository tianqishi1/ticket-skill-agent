"""mcp_file_server.py
=====================
独立的 FastMCP 只读文件服务 —— 故障排查智能体的证据读取后端。

【安全设计 —— 代码层硬限制，不依赖大模型 prompt 做安全防护】
1. 仅暴露 3 个只读工具：read_file / glob_list / grep_search；
2. 代码层面禁止一切写文件、删除、执行 shell、发起网络请求；
   本文件内不 import os.system / subprocess / shutil / socket / requests 等危险能力，
   也不提供任何可被复用做这些事的函数；
3. 路径白盒校验：所有传入路径 resolve() 后必须落在项目允许目录内，
   允许目录限定为仓库根目录下的 sample_data/ 与 src/；
   任何向上跳出项目目录的路径（含 .. 穿越、绝对路径越界、符号链接外指）
   一律返回 PermissionError，不读取任何内容；
4. 单文件读取有大小上限，避免超大文件耗尽内存；
5. 输出全部走 stdout（MCP stdio 传输），诊断信息走 stderr，不污染协议。

运行方式
--------
- 作为独立 MCP Server 进程运行（stdio 传输）：python mcp_file_server.py
- 由 agent/nodes.py 中的 MCPFileClient 通过 stdio 子进程拉起并调用。

注意：本服务只读，任何节点都不可通过它修改文件系统。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# FastMCP 在 mcp 2.x 起已拆分为独立 fastmcp 包；
# 这里从 fastmcp 导入 FastMCP（与 mcp 1.x 客户端协议互通）。
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 路径白盒配置：允许访问的根目录（仓库根目录下的 sample_data/ 与 src/）
# 本文件位于 <repo_root>/langgraph_demo/mcp_file_server.py
# 因此 repo_root = 本文件路径的上两级。
# ---------------------------------------------------------------------------
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# 允许访问的绝对目录前缀（resolve 后精确比较，杜绝穿越）
ALLOWED_BASE_DIRS: tuple[Path, ...] = (
    (REPO_ROOT / "sample_data").resolve(),
    (REPO_ROOT / "src").resolve(),
)

# 单文件读取上限（256 KiB），超过则截断，防止超大日志耗尽上下文
MAX_READ_BYTES: int = 256 * 1024

# glob 单次返回文件数上限
MAX_GLOB_RESULTS: int = 50

# grep 单次返回匹配行上限
MAX_GREP_LINES: int = 200

# 创建 FastMCP 实例
mcp: FastMCP = FastMCP("ticket-readonly-fs")


def _resolve_allowed(path: Path) -> Path:
    """将路径 resolve 后校验是否落在允许目录内，越界直接抛 PermissionError。

    - 相对路径以仓库根目录解析；
    - resolve() 会规范化 .. 与符号链接，结果再做白盒比对；
    - 必须严格落在某个允许目录内（允许等于该目录本身或其子路径）。
    """
    if not path.is_absolute():
        path = REPO_ROOT / path
    # strict=False 允许目标尚不存在时也能解析出规范化绝对路径（用于 glob 校验）
    resolved = path.resolve()

    for base in ALLOWED_BASE_DIRS:
        try:
            # relative_to 成功 -> resolved 位于 base 内（含等于 base 本身）
            resolved.relative_to(base)
            return resolved
        except ValueError:
            continue
    # 越界：绝不返回内容，直接抛错由 MCP 框架回传给调用方
    raise PermissionError(
        f"[安全拦截] 路径越界，拒绝访问：{path}（resolve 后为 {resolved}，"
        f"不在允许目录 {ALLOWED_BASE_DIRS} 内）"
    )


def _is_within_allowed(path: Path) -> bool:
    """只读判定，用于 glob 结果二次校验。"""
    try:
        _resolve_allowed(path)
        return True
    except PermissionError:
        return False


@mcp.tool()
def read_file(file_path: str) -> str:
    """读取单个文本文件内容（只读，超过 256KiB 截断）。

    Args:
        file_path: 文件路径，可为绝对路径或相对仓库根的相对路径；
                   必须落在 sample_data/ 或 src/ 允许目录内，否则拒绝访问。
    Returns:
        文件文本内容（UTF-8，超长截断并加提示前缀）。
    """
    p = _resolve_allowed(Path(file_path))
    # 不允许把目录当文件读
    if p.is_dir():
        return f"[错误] 目标是目录而非文件：{p}"
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    data = p.read_bytes()
    if len(data) > MAX_READ_BYTES:
        head = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        return f"[文件过大，已截断显示前 {MAX_READ_BYTES} 字节]\n{head}"
    return data.decode("utf-8", errors="replace")


@mcp.tool()
def glob_list(base_dir: str, pattern: str = "**/*") -> str:
    """递归列出某允许目录下匹配 pattern 的文件（只读，不返回目录）。

    Args:
        base_dir: 起始目录，必须在 sample_data/ 或 src/ 允许目录内。
        pattern: glob 模式，默认列出全部文件。
    Returns:
        每行一个相对仓库根的路径，最多 MAX_GLOB_RESULTS 条。
    """
    base = _resolve_allowed(Path(base_dir))
    if not base.exists():
        return f"[错误] 目录不存在：{base}"
    if not base.is_dir():
        return f"[错误] 目标不是目录：{base}"
    # 防御：拒绝明显带 .. 的模式（glob 本身不会上跳，但做兜底）
    if ".." in pattern.split("/"):
        return "[错误] pattern 不允许包含 .. "
    files: list[str] = []
    for hit in base.glob(pattern):
        if not hit.is_file():
            continue
        # 二次校验结果路径仍在允许目录内（防止符号链接外指）
        if not _is_within_allowed(hit):
            continue
        files.append(str(hit.resolve().relative_to(REPO_ROOT.resolve())))
        if len(files) >= MAX_GLOB_RESULTS:
            files.append("...（结果过多，已截断）")
            break
    return "\n".join(files) if files else "[无匹配文件]"


@mcp.tool()
def grep_search(pattern: str, file_path: str) -> str:
    """在单个允许文件内做正则搜索，返回带行号的匹配行（只读）。

    Args:
        pattern: 正则表达式。
        file_path: 目标文件，必须在 sample_data/ 或 src/ 允许目录内。
    Returns:
        形如 `行号: 内容` 的匹配行，最多 MAX_GREP_LINES 行。
    """
    p = _resolve_allowed(Path(file_path))
    if p.is_dir():
        return f"[错误] 目标是目录而非文件：{p}"
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"[错误] 非法正则：{exc}"
    text = p.read_text(encoding="utf-8", errors="replace")
    out: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if rx.search(line):
            out.append(f"{idx}: {line}")
            if len(out) >= MAX_GREP_LINES:
                out.append("...（匹配过多，已截断）")
                break
    return "\n".join(out) if out else "[无匹配]"


def _print_safety_banner() -> None:
    """启动时向 stderr 输出安全边界信息（不污染 stdout 协议）。"""
    # Windows 下尽量让中文不乱码（stderr 同样需要 UTF-8）
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
        try:
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    print("=" * 60, file=sys.stderr)
    print("FastMCP 只读文件服务已启动", file=sys.stderr)
    print(f"允许访问根目录：{[str(p) for p in ALLOWED_BASE_DIRS]}", file=sys.stderr)
    print("暴露工具：read_file / glob_list / grep_search（仅只读）", file=sys.stderr)
    print("禁止：写文件 / 删除 / 执行 shell / 网络请求", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


if __name__ == "__main__":
    _print_safety_banner()
    # FastMCP 默认使用 stdio 传输，由 MCPFileClient 子进程拉起
    mcp.run()
