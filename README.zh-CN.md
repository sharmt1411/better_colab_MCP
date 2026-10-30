本仓库由 [学AI,上L站!](Linux.do) 赞助提供

# Colab MCP

本项目是一个本地优先的 MCP 服务，用来把 Google Colab 作为开发、Shell、文件传输和训练运行时来控制。

English documentation: [README.md](README.md)

## 这个库提供什么

这个 fork 在上游静态 Colab MCP 基线之上增加了：

- notebook 单元格编辑、批量执行、状态轮询和输出读取
- 受控 Edge 浏览器启动和 Colab 前端 MCP 连接修复
- 显式 Python runtime 连接工具 `connect_runtime`
- MCP 原生运行时加速器切换，包括 T4 GPU
- 基于 Colab Terminal 的 shell 命令和后台任务
- 本地路径到 Colab runtime 的上传/下载工具
- runtime 文件 list/stat/delete/mkdir 工具
- GPU 检查、资源快照和 GPU monitor
- 环境变量工具，以及 runtime restart/shutdown

## 🌟 本 Fork 的核心改进与特色 (Enhancements)

本项目在开源基线基础上，针对 **AI 代理（LLM Agent）自动化控制 Google Colab** 的实际生产痛点，进行了多项高可用与性能重构：

1. **🚀 稳定持久的会话连接（Session Resilience）**
   - **CDP 导航防抖**：连接时智能识别目标 Notebook URL，若当前标签页已挂载则复用页面，杜绝无意义的页面重刷与会话重置。
   - **5 秒断线自动重试**：针对 Colab 闲置休眠或瞬时网络抖动，提供 5 秒自动重试机制，大幅提升过夜挂机与长时间训练的恢复成功率。
   - **全自动化流程体验**：消除了误导性的人工授权等待提示（Waiting for user connect），改用明确的底层状态日志，避免大模型误以为需人工介入。

2. **📉 极致精简的 Token 消耗（Token-Optimized Logging）**
   - **日志智能尾部截取**：`read_cell_outputs` 默认截取最新 20 行，避免大模型被成百上千行的进度条挤爆上下文。
   - **Payload 三重冗余消除**：彻底移除原版中 `outputs`、`tailText`、`chunks` 三份完全重复打包的冗余设计，默认只返回精简纯文本（`tailText`），**Token 消耗立减 70%**。同时支持 `includeRaw=True` 按需提取富媒体。

3. **🎯 精准的代码与环境控制**
   - **支持指定 Notebook URL**：支持在 `open_colab_browser_connection` 中显式指定目标笔记本，杜绝被强制重定向到 scratchpad 草稿本。
   - **高精度单元格修补（`patch_cell`）**：实现了基于搜索替换（Search-and-Replace）的高精度代码修补，无需重写整个单元格。
   - **无限制文件传输**：移除了本地文件路径白名单限制，支持本地任意合法路径与 Colab 间的文件自由同步。

## 💡 推荐开启的核心工具（16 Tools，其余按需开启）

Colab MCP 提供了 50+ 个细粒度工具，如果全量开启，**每个轮次的 System Prompt 都会被注入大量重复的 Tool Schema，严重浪费 Token 且易引发模型决策犹豫与幻觉**。

经过实战检验，**强烈建议在客户端仅启用以下 16 个核心工具**（满足 99% 的日常交互与挂机训练），其余高级工具按需启用：

| 类别 | 推荐启用的核心工具 | 核心职责与说明 |
| :--- | :--- | :--- |
| **连接与初始化** | `open_colab_browser_connection` | 挂载/打开浏览器与 CDP 调试通道 |
| | `connect_runtime` | 显式连接并初始化 Colab Python 计算实例 |
| | `check_runtime` | 检查 Python 运行时是否就绪、环境包信息 |
| **生命周期管理** | `restart_runtime` | 重启 Python 运行时（快速释放显存/内存） |
| | `shutdown_runtime` | 训练/任务完成后彻底释放 Colab 实例（避免资源浪费） |
| **硬件状态** | `check_gpu` | 检查当前分配的 GPU 状态与可用性 |
| **单元格增删改** | `get_cells` | 获取 Notebook 完整结构及所有 Cell ID |
| | `add_code_cell` | 创建新的 Python 代码单元格 |
| | `add_text_cell` | 创建新的 Markdown 说明单元格 |
| | `delete_cell` | 删除冗余/无用的单元格 |
| | `update_cell` | 全量更新单元格代码 |
| | `patch_cell` | **核心推荐**：通过 Search-and-Replace 局部精确修补代码 |
| **代码执行与控制** | `run_code_cell` | 执行指定的代码单元格 |
| | `cancel_queued_cells` | 取消排队中或卡死的代码执行队列 |
| | `read_cell_outputs` | **核心推荐**：读取单元格最新执行日志（默认 20 行精简纯文本） |
| **万能终端与文件** | `run_shell_command` | 执行 Linux 终端命令（pip 安装、文件查看、系统命令等） |

> **为什么建议关闭其他 30+ 个工具？**
> 1. **功能重叠**：`run_code_cells`、`run_cell_range`、`run_all_cells` 容易造成批量级联错误，逐个调用 `run_code_cell` 粒度更可控；`get_cell` / `find_cells` 完全能被 `get_cells` 代替。
> 2. **Shell 万能替代**：文件操作（`list_files`、`make_directory`、`delete_file`、`stat_file`）以及环境变量（`set_env_vars`、`load_env_file`），在 `run_shell_command` 中通过常规 Linux 命令（`ls`, `mkdir`, `rm`, `export`）更加灵活直接，完全无需专用 MCP 工具常驻上下文。
> 3. **极简上下文**：将工具数从 50+ 精简至 16 个，每次对话可直接**节约数千 Token**，模型响应更快、出错率更低。


## 项目结构

- `src/colab_mcp/`: Python MCP 服务、Colab session proxy、runtime/browser 工具。
- `tests/`: proxy、tool 行为和 websocket server 单测。
- `docs/`: 使用说明、工具清单、示例、故障排查和 API 约定。
- `TODO.md`: 实现清单和后续事项。

## 安装和启动

```powershell
uv sync --dev
uv run pytest
```

启动 MCP 服务：

```powershell
uv run colab-mcp
```

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "colab-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "<path-to-colab_mcp>", "colab-mcp"],
      "startup_timeout_sec": 120,
      "env": {
        "COLAB_MCP_EDGE_CDP_PORT": "9333",
        "COLAB_MCP_EDGE_URL_CONTAINS": "colab.research.google.com"
      }
    }
  }
}
```

把 `<path-to-colab_mcp>` 替换成本地仓库的绝对路径。

## Runtime 就绪状态

不要因为浏览器 MCP 已连接，就假设 Python runtime 已经可用。Shell、文件、GPU 和训练工具都需要真实的 Colab runtime 和 terminal socket。

## 返回结果约定

工具会返回结构化状态：

- `ok=true`, `status="ok"`: 操作完成，且没有已知 warning。
- `ok=false`, `status="warning"`: 前置条件缺失、部分成功、noop 或可恢复状态。必须读取 `warnings` 和 `recommendedNextActions`。
- `ok=false`, `status="error"`: 执行失败、超时、命令非 0 退出，或工具发生明确错误。重试前先检查 `error`、`stdout`、`stderr` 和 `recommendedNextActions`。

遇到 `warning` 或 `error` 不要继续硬跑后续流程。

## 浏览器和 Runtime 流程

这里有两层连接：

1. 浏览器/前端 MCP 连接。
2. Colab Python runtime 连接。

普通 runtime 工作流：

```text
open_colab_browser_connection()
connect_runtime(waitSeconds=180)
```

GPU 工作流：

```text
open_colab_browser_connection()
set_runtime_accelerator(accelerator="T4 GPU", apply=true)
open_colab_browser_connection()
connect_runtime(waitSeconds=180)
check_gpu()
```

切换加速器可能会重启或断开 runtime。每次切换后，都要重新连接浏览器 MCP，然后调用 `connect_runtime`，再运行 shell、文件、GPU 或训练工具。

## 常用训练流程

完整训练流程可以全部通过 MCP 完成：

```text
open_colab_browser_connection()
set_runtime_accelerator(accelerator="T4 GPU", apply=true)
open_colab_browser_connection()
connect_runtime(waitSeconds=180)
check_gpu()
upload_local_file(localPath="<path-to-train.py>", path="/content/train.py", overwrite=true)
start_background_command(
  name="train",
  command="python /content/train.py",
  logPath="/content/train.log",
  cwd="/content"
)
watch_background_command(name="train", lines=100)
download_file_to_local(path="/content/model.pt", localPath="<path-to-model.pt>", overwrite=true)
shutdown_runtime(reason="training finished")
```

训练和长任务必须优先用 `start_background_command`。不要用 `run_shell_command` 跑训练；它只适合短命令。

## 释放或关闭 Runtime 实例

训练完成、任务取消，或者不再需要 CPU/GPU 资源时，必须释放 Colab runtime：

```text
shutdown_runtime(reason="training finished")
```

推荐的训练完成收尾流程：

```text
stat_file(path="/content/model.pt")
download_file_to_local(path="/content/model.pt", localPath="<path-to-model.pt>", overwrite=true)
shutdown_runtime(reason="training finished")
```

推荐的取消任务流程：

```text
check_background_command(name="train")
stop_background_command(name="train")
shutdown_runtime(reason="training cancelled")
```

注意：

- 先下载权重、日志和其他产物，再关闭 runtime。`/content` 下的文件在 runtime 释放后可能丢失。
- `shutdown_runtime` 释放/断开的是当前 Colab CPU/GPU runtime 实例。它不是卸载 MCP 服务，也不是关闭浏览器标签页。
- 关闭后如果还要继续运行，需要重新调用 `open_colab_browser_connection()` 和 `connect_runtime(waitSeconds=180)`。

## 本地文件传输

在本机和 Colab 之间传文件时，优先使用路径级工具：

```text
upload_local_file(localPath="<local-file>", path="/content/input.txt", overwrite=true)
download_file_to_local(path="/content/output.txt", localPath="<local-output>", overwrite=true)
```

base64 工具仍然保留，适合调用方已经有内存内容的情况：

```text
upload_file(path="/content/input.txt", contentBase64="...")
download_file(path="/content/output.txt")
upload_file_chunk(...)
download_file_chunk(...)
complete_upload(...)
```

## 工具分组

初始化和指南：

- `get_connection_info`

浏览器、runtime 和加速器：

- `open_colab_browser_connection`
- `connect_runtime`
- `check_runtime`
- `restart_runtime`
- `shutdown_runtime`
- `set_runtime_accelerator`

Shell 和长任务：

- `run_shell_command`
- `start_background_command`
- `check_background_command`
- `watch_background_command`
- `list_background_commands`
- `stop_background_command`
- `tail_file`

Runtime 文件：

- `upload_local_file`
- `download_file_to_local`
- `upload_file`
- `download_file`
- `upload_file_chunk`
- `complete_upload`
- `download_file_chunk`
- `stat_file`
- `list_files`
- `make_directory`
- `delete_file`

GPU 和资源监控：

- `check_gpu`
- `resource_usage_snapshot`
- `sample_gpu_usage`
- `start_gpu_monitor`
- `read_gpu_monitor`
- `stop_gpu_monitor`

Notebook 单元格：

- `get_cells`
- `get_cell`
- `add_code_cell`
- `add_text_cell`
- `update_cell`
- `patch_cell`
- `delete_cell`
- `move_cell`
- `find_cells`
- `replace_cells`
- `run_code_cell`
- `run_code_cells`
- `run_cell_range`
- `run_all_cells`
- `cancel_queued_cells`
- `get_cell_status`
- `wait_for_cells`
- `read_cell_outputs`
- `watch_cell_outputs`

环境变量：

- `set_env_vars`
- `get_env_vars`
- `unset_env_vars`
- `load_env_file`
- `rerun_env_setup_cells`

Notebook 导入导出：

- `import_notebook`
- `export_notebook`
- `upload_notebook` 是 `import_notebook` 的 alias
- `download_notebook` 是 `export_notebook` 的 alias

## 浏览器控制

`open_colab_browser_connection` 可以启动专用 Microsoft Edge，打开 Colab，并连接 Colab 前端到本地 MCP 服务。

默认值：

- CDP 端口：`9333`，可用 `COLAB_MCP_EDGE_CDP_PORT` 覆盖
- Edge profile：`~/.codex/edge-colab-mcp-profile`，可用 `COLAB_MCP_EDGE_PROFILE` 覆盖
- Edge 可执行文件：自动检测，可用 `COLAB_MCP_EDGE_PATH` 覆盖

`colabctl` 仍然保留，用于诊断和手动修复：

```powershell
uv run colabctl status
uv run colabctl connect
uv run colabctl smoke-mcp
uv run colabctl set-accelerator --accelerator GPU
```

正常自动化流程应该优先使用 MCP 工具，而不是 `colabctl`。

## 验证

本地测试：

```powershell
uv run pytest
uv run python -m compileall src
```

当前流程已经用完整 MCP-only ResCNN smoke 验证过：

- 通过 MCP 选择 T4
- 通过 MCP 连接 Python runtime
- 用 `check_gpu` 确认 `Tesla T4`
- 上传本地训练脚本
- 用 `start_background_command` 训练
- 用 `watch_background_command` 查看日志
- 用 `download_file_to_local` 下载权重
- 用 `shutdown_runtime` 关闭 runtime

## 文档

- [docs/USAGE.md](docs/USAGE.md): 使用说明
- [docs/TOOLS.md](docs/TOOLS.md): 工具清单
- [docs/EXAMPLES.md](docs/EXAMPLES.md): 常见示例
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): 故障排查
- [docs/API_CONVENTIONS.md](docs/API_CONVENTIONS.md): schema 和返回约定

