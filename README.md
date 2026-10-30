The Repositories was supported by Linux.do

# Colab MCP

Local-first MCP server for controlling Google Colab as a development, shell, file, and training runtime.

Chinese documentation: [README.zh-CN.md](README.zh-CN.md)

## What This Provides

This fork extends the upstream static Colab MCP baseline with:

- notebook editing, batch execution, status polling, and output reading
- controlled Edge browser startup and Colab frontend MCP repair
- explicit Python runtime connection with `connect_runtime`
- MCP-native runtime accelerator switching, including T4 GPU selection
- Colab Terminal-backed shell commands and background jobs
- local-path file upload/download tools
- runtime file list/stat/delete/directory tools
- GPU checks, snapshots, and monitor jobs
- environment variable helpers and runtime restart/shutdown tools

## 🌟 Key Enhancements & Differences from Upstream

This fork builds upon the open-source baseline and addresses critical production issues encountered when using **AI Agents to control Google Colab**:

1. **🚀 Session Resilience & Automation**
   - **CDP Navigation Debounce**: Intelligently identifies the target notebook URL. If already active, it reuses the tab without redundant reloads, preventing session drops.
   - **5s Automatic Retry**: Implements an automatic 5-second retry loop on initial connection failures, significantly boosting overnight and long-running job recovery.
   - **Fully Automated Experience**: Replaced misleading manual authorization prompts (`Waiting for user to connect`) with clear automated status messages.

2. **📉 Token-Optimized Logging**
   - **Line Numbers & Recommended 20 Lines**: `read_cell_outputs` returns clean outputs formatted with line numbers (`123: log`) for pinpoint error location. Recommended to start by reading the latest 20 lines.
   - **ANSI Stripping & Runaway Line Protection**: Automatically strips terminal ANSI color escape codes (eliminating `\u001b[35m` garbage) and gracefully folds single lines over 1000 characters (e.g. giant state dicts/lists).
   - **Triple Payload Deduplication**: Eliminated redundant representations (`outputs`, `tailText`, `chunks` duplicate logging), defaulting to clean, streamlined `tailText` and **reducing token consumption by ~70%**. Raw Jupyter objects remain available on-demand with `includeRaw=true`.

3. **🎯 Precise Code & Environment Control**
   - **Target Notebook URL Support**: Allows binding to specific notebook URLs directly via `open_colab_browser_connection`, preventing unwanted redirects to scratchpads.
   - **Search-and-Replace Cell Patching**: Enhanced `patch_cell` with accurate search-and-replace mechanics rather than fragile line-based replacements.
   - **Unrestricted Local File Paths**: Removed arbitrary local path restrictions, allowing seamless file transfers across any local directory and Colab.

## 💡 Recommended Core Tools (16 Tools — Enable Others on Demand)

Colab MCP defines over 50 tools. Enabling all of them simultaneously injects excessive schemas into the system prompt on every turn, consuming thousands of tokens and increasing model decision friction.

In practice, **we strongly recommend enabling only the 16 core tools below**, leaving secondary tools disabled unless specifically required:

| Category | Recommended Core Tool | Purpose |
| :--- | :--- | :--- |
| **Connection & Setup** | `open_colab_browser_connection` | Mount/open browser tab and CDP debug port |
| | `connect_runtime` | Initialize Colab Python compute runtime |
| | `check_runtime` | Verify Python runtime health & environment packages |
| **Lifecycle** | `restart_runtime` | Restart Python runtime (release GPU VRAM / memory) |
| | `shutdown_runtime` | Fully release Colab instance after training/tasks |
| **Hardware** | `check_gpu` | Check active GPU model and availability |
| **Notebook Authoring** | `get_cells` | Inspect full notebook structure and all cell IDs |
| | `add_code_cell` | Add new Python code cell |
| | `add_text_cell` | Add new Markdown text cell |
| | `delete_cell` | Remove unwanted cells |
| | `update_cell` | Full update of cell source |
| | `patch_cell` | **Recommended**: Surgical search-and-replace modification |
| **Execution & Output** | `run_code_cell` | Execute a single code cell safely |
| | `cancel_queued_cells` | Cancel running or queued cells |
| | `read_cell_outputs` | **Recommended**: Inspect tailed execution logs with line numbers (e.g. 123: log, recommended: 20 lines) |
| **Universal Shell** | `run_shell_command` | Execute Linux commands (pip install, file checks, bash scripts) |

> **Why disable the other 30+ tools?**
> 1. **Redundancy**: Batch operations (`run_all_cells`, `run_cell_range`) can cause cascading failures; granular execution via `run_code_cell` is much safer for AI agents. `get_cells` already covers single cell lookups.
> 2. **Shell Versatility**: Dedicated file tools (`list_files`, `make_directory`, `delete_file`) and environment tools (`set_env_vars`) are effortlessly handled with standard Linux commands via `run_shell_command` (`ls`, `mkdir`, `rm`, `export`).
> 3. **Token Efficiency**: Pruning 50+ tools down to 16 **saves thousands of system prompt tokens** per turn, reduces hallucination, and speeds up agent response time.


## Project Layout

- `src/colab_mcp/`: Python MCP server, Colab session proxy, runtime/browser tools.
- `tests/`: unit tests for proxy, tool behavior, and websocket server behavior.
- `docs/`: usage, tool inventory, examples, troubleshooting, and API conventions.
- `TODO.md`: implementation checklist and follow-up notes.

## Setup

```powershell
uv sync --dev
uv run pytest
```

Run the MCP server:

```powershell
uv run colab-mcp
```

MCP client configuration:

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

Replace `<path-to-colab_mcp>` with the absolute path to your local checkout.

## Runtime Readiness

Do not assume the Python runtime is ready just because the browser MCP is connected. Shell, file, GPU, and training tools require a real Colab runtime and terminal socket.

## Result Contract

Tools use structured status fields:

- `ok=true`, `status="ok"`: operation completed without known warnings.
- `ok=false`, `status="warning"`: prerequisite missing, partial/no-op result, or recoverable state. Read `warnings` and `recommendedNextActions`.
- `ok=false`, `status="error"`: execution failed, timed out, returned a non-zero exit code, or hit an explicit tool error. Inspect `error`, `stdout`, `stderr`, and `recommendedNextActions` before retrying.

Do not continue a workflow after `warning` or `error` without handling the returned instructions.

## Browser And Runtime Flow

There are two separate connection layers:

1. Browser/frontend MCP connection.
2. Colab Python runtime connection.

For normal runtime work:

```text
open_colab_browser_connection()
connect_runtime(waitSeconds=180)
```

For GPU work:

```text
open_colab_browser_connection()
set_runtime_accelerator(accelerator="T4 GPU", apply=true)
open_colab_browser_connection()
connect_runtime(waitSeconds=180)
check_gpu()
```

Changing the accelerator can restart or disconnect the runtime. Always reconnect the browser MCP and then call `connect_runtime` before shell, file, GPU, or training tools.

## Common Training Flow

All core training steps can run through MCP tools:

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

Use `start_background_command` for training and long jobs. Do not use `run_shell_command` for training; it is for short bounded commands.

## Release Or Close The Runtime Instance

Always release the Colab runtime when work is finished, cancelled, or no longer needs CPU/GPU resources:

```text
shutdown_runtime(reason="training finished")
```

Recommended completion flow:

```text
stat_file(path="/content/model.pt")
download_file_to_local(path="/content/model.pt", localPath="<path-to-model.pt>", overwrite=true)
shutdown_runtime(reason="training finished")
```

Recommended cancellation flow:

```text
check_background_command(name="train")
stop_background_command(name="train")
shutdown_runtime(reason="training cancelled")
```

Important notes:

- Download weights, logs, and other artifacts before shutdown. Files under `/content` can be lost after the runtime is released.
- `shutdown_runtime` releases/disconnects the active Colab CPU/GPU runtime instance. It does not uninstall this MCP server and does not close the browser tab.
- After shutdown, future runtime work must reconnect with `open_colab_browser_connection()` and `connect_runtime(waitSeconds=180)`.

## Local File Transfer

Prefer local-path tools when moving files between this machine and Colab:

```text
upload_local_file(localPath="<local-file>", path="/content/input.txt", overwrite=true)
download_file_to_local(path="/content/output.txt", localPath="<local-output>", overwrite=true)
```

Base64 tools remain available for clients that already have content in memory:

```text
upload_file(path="/content/input.txt", contentBase64="...")
download_file(path="/content/output.txt")
upload_file_chunk(...)
download_file_chunk(...)
complete_upload(...)
```

## Tool Groups

Connection:

- `get_connection_info`

Browser, runtime, and accelerator:

- `open_colab_browser_connection`
- `connect_runtime`
- `check_runtime`
- `restart_runtime`
- `shutdown_runtime`
- `set_runtime_accelerator`

Shell and long jobs:

- `run_shell_command`
- `start_background_command`
- `check_background_command`
- `watch_background_command`
- `list_background_commands`
- `stop_background_command`
- `tail_file`

Runtime files:

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

GPU and resource monitoring:

- `check_gpu`
- `resource_usage_snapshot`
- `sample_gpu_usage`
- `start_gpu_monitor`
- `read_gpu_monitor`
- `stop_gpu_monitor`

Notebook cells:

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

Environment:

- `set_env_vars`
- `get_env_vars`
- `unset_env_vars`
- `load_env_file`
- `rerun_env_setup_cells`

Notebook import/export:

- `import_notebook`
- `export_notebook`
- `upload_notebook` alias for `import_notebook`
- `download_notebook` alias for `export_notebook`

## Browser Control

`open_colab_browser_connection` can start a dedicated Microsoft Edge instance, open Colab, and connect the Colab frontend to the local MCP server.

Defaults:

- CDP port: `9333`, override with `COLAB_MCP_EDGE_CDP_PORT`
- Edge profile: `~/.codex/edge-colab-mcp-profile`, override with `COLAB_MCP_EDGE_PROFILE`
- Edge executable: auto-detected, override with `COLAB_MCP_EDGE_PATH`

`colabctl` remains available for diagnostics and manual repair:

```powershell
uv run colabctl status
uv run colabctl connect
uv run colabctl smoke-mcp
uv run colabctl set-accelerator --accelerator GPU
```

Normal automation should prefer MCP tools over `colabctl`.

## Verification

Run local tests:

```powershell
uv run pytest
uv run python -m compileall src
```

The current flow has been validated with a full MCP-only ResCNN smoke:

- selected T4 through MCP
- connected the Python runtime through MCP
- confirmed `Tesla T4` with `check_gpu`
- uploaded a local training script
- trained with `start_background_command`
- watched logs with `watch_background_command`
- downloaded weights with `download_file_to_local`
- shut down the runtime with `shutdown_runtime`

## Documentation

- [docs/USAGE.md](docs/USAGE.md): usage notes
- [docs/TOOLS.md](docs/TOOLS.md): tool inventory
- [docs/EXAMPLES.md](docs/EXAMPLES.md): common examples
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): recovery guidance
- [docs/API_CONVENTIONS.md](docs/API_CONVENTIONS.md): schema and result conventions

Thanks to the linux.do laoyou for their support. "Laoyou" is the community's own friendly term for respected peers.

## Privacy Notes

The README uses placeholders such as `<path-to-colab_mcp>` and does not include local usernames, personal checkout paths, tokens, or runtime artifacts. Generated training artifacts should stay under `artifacts/`, which is ignored by git.
