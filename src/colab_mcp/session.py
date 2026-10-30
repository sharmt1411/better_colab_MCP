# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import base64
from copy import deepcopy
from collections.abc import AsyncIterator
import contextlib
from contextlib import AsyncExitStack
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from itertools import count
from fastmcp import FastMCP, Client
from fastmcp.client.transports import ClientTransport
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
from fastmcp.server.proxy import FastMCPProxy
from fastmcp.tools.tool import Tool, ToolResult
from mcp.client.session import ClientSession
from mcp.types import TextContent
from pydantic import PrivateAttr
from websockets.sync.client import connect as websocket_connect

from colab_mcp.websocket_server import ColabWebSocketServer, COLAB, SCRATCH_PATH

UI_CONNECTION_TIMEOUT = 25.0  # secs

FE_CONNECTED_KEY = "fe_connected"
PROXY_TOKEN_KEY = "proxy_token"
PROXY_PORT_KEY = "proxy_port"
INJECTED_TOOL_NAME = "open_colab_browser_connection"
ENV_SETUP_MARKER_PREFIX = "# COLAB_MCP_ENV_SETUP:"
EDGE_CDP_PORT_ENV = "COLAB_MCP_EDGE_CDP_PORT"
EDGE_CDP_URL_CONTAINS_ENV = "COLAB_MCP_EDGE_URL_CONTAINS"
EDGE_PROFILE_ENV = "COLAB_MCP_EDGE_PROFILE"
EDGE_PATH_ENV = "COLAB_MCP_EDGE_PATH"
DEFAULT_EDGE_CDP_PORT = "9333"
DEFAULT_EDGE_PROFILE = Path.home() / ".codex" / "edge-colab-mcp-profile"
_TERMINAL_COMMAND_LOCK: asyncio.Lock | None = None


def _terminal_command_lock() -> asyncio.Lock:
    global _TERMINAL_COMMAND_LOCK
    if _TERMINAL_COMMAND_LOCK is None:
        _TERMINAL_COMMAND_LOCK = asyncio.Lock()
    return _TERMINAL_COMMAND_LOCK


COLAB_STATIC_TOOL_SCHEMAS = {
    "add_code_cell": {
        "description": "Inserts a code type cell at the provided index and shifts existing cells. The resulting new cell id is returned.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIndex": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "The index at which to insert the cell.",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "r", "julia"],
                    "description": "The programming language of the new cell.",
                },
                "code": {
                    "type": "string",
                    "description": "The code content of the new cell.",
                },
            },
            "required": ["cellIndex", "language", "code"],
            "additionalProperties": False,
        },
    },
    "add_text_cell": {
        "description": "Inserts a text type cell at the provided index and shifts existing cells. The resulting new cell id is returned.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIndex": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "The index at which to insert the cell.",
                },
                "content": {
                    "type": "string",
                    "description": "The content of the new cell. This can include Markdown and LaTeX syntax.",
                },
            },
            "required": ["cellIndex", "content"],
            "additionalProperties": False,
        },
    },
    "delete_cell": {
        "description": "Deletes the cell with the provided cell ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellId": {
                    "type": "string",
                    "description": "The ID of the cell to delete.",
                },
            },
            "required": ["cellId"],
            "additionalProperties": False,
        },
    },
    "get_cells": {
        "description": "Gets a range of cells as JSON from the notebook.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIndexStart": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "The starting index for the cell range (inclusive). If not provided, this defaults to 0.",
                },
                "cellIndexEnd": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "The end index for the cell range (inclusive). This must be greater than or equal to cellIndexStart. If not provided, this defaults to the last available cell index.",
                },
                "includeOutputs": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to include the code cell execution outputs in the response. If not provided, this defaults to false.",
                },
            },
            "additionalProperties": False,
        },
    },
    "move_cell": {
        "description": "Moves a cell to the provided index and shifts existing cells.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellId": {
                    "type": "string",
                    "description": "The ID of the cell to move.",
                },
                "cellIndex": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "The index to move the cell to.",
                },
            },
            "required": ["cellId", "cellIndex"],
            "additionalProperties": False,
        },
    },
    "run_code_cell": {
        "description": "Executes the code in the cell with the provided cell ID. The cell must be a code cell type. The output of the cell execution is returned.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellId": {
                    "type": "string",
                    "description": "The ID of the code cell to execute.",
                },
            },
            "required": ["cellId"],
            "additionalProperties": False,
        },
    },
    "update_cell": {
        "description": "Overwrites the contents of the cell with the provided new content. The cell must already exist and is identified by its cell ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellId": {
                    "type": "string",
                    "description": "The ID of the cell to update.",
                },
                "content": {
                    "type": "string",
                    "description": "The new content of the cell.",
                },
            },
            "required": ["cellId", "content"],
            "additionalProperties": False,
        },
    },
}


COLAB_MANAGEMENT_TOOL_SCHEMAS = {
    "set_env_vars": {
        "description": "Sets environment variables in the current Colab Python runtime by executing a generated setup cell.",
        "parameters": {
            "type": "object",
            "properties": {
                "variables": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Environment variable names and values to set.",
                },
                "persist": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, keep the generated setup cell in the notebook. If false, delete it after execution while leaving the runtime environment variables set.",
                },
                "markerName": {
                    "type": "string",
                    "default": "default",
                    "description": "Marker name used to find this setup cell later with rerun_env_setup_cells.",
                },
                "cellIndex": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "Optional insertion index for the generated setup cell. Defaults to the end of the notebook.",
                },
            },
            "required": ["variables"],
            "additionalProperties": False,
        },
    },
    "restart_runtime": {
        "description": "Restarts the current Colab Python runtime. The notebook page stays open, but the runtime state and environment variables are reset.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional text printed before the restart.",
                },
            },
            "additionalProperties": False,
        },
    },
    "shutdown_runtime": {
        "description": (
            "Disconnects and releases/unassigns the active Colab CPU/GPU runtime "
            "instance. Call this as the final cleanup step after training or long "
            "runtime work is complete or cancelled. Download needed weights, logs, "
            "and artifacts first because /content files can be lost after release. "
            "This does not uninstall the MCP server and does not close the browser tab."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional cleanup reason printed before releasing the Colab runtime, for example 'training finished' or 'training cancelled'.",
                },
            },
            "additionalProperties": False,
        },
    },
    "rerun_env_setup_cells": {
        "description": "Reruns persisted environment setup cells created by set_env_vars with persist=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "markerName": {
                    "type": "string",
                    "description": "Optional marker name to rerun. If omitted, reruns all persisted Colab MCP env setup cells.",
                },
                "includeOutputs": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include execution outputs from rerun setup cells in the response.",
                },
            },
            "additionalProperties": False,
        },
    },
    "check_runtime": {
        "description": "Checks the current Colab runtime Python, platform, CPU, working directory, and selected package versions.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "connect_runtime": {
        "description": "Uses the controlled Colab browser to connect or reconnect the Colab Python runtime, then waits until terminal-backed runtime tools can see kernel.runtime.",
        "parameters": {
            "type": "object",
            "properties": {
                "waitSeconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 180,
                    "description": "Maximum seconds to wait for Colab to expose a real Python runtime after clicking Connect/Reconnect.",
                },
            },
            "additionalProperties": False,
        },
    },
    "check_gpu": {
        "description": "Checks actual GPU availability in the connected Python runtime using nvidia-smi. Call open_colab_browser_connection and connect_runtime first. If no GPU is active this returns status=warning with remediation steps; do not rely on UI text alone.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "set_runtime_accelerator": {
        "description": "Uses the controlled Colab browser to choose an accelerator such as T4 GPU. Applying may restart/disconnect the runtime; always call open_colab_browser_connection, connect_runtime, then check_gpu afterwards.",
        "parameters": {
            "type": "object",
            "properties": {
                "accelerator": {
                    "type": "string",
                    "default": "T4 GPU",
                    "description": "Visible accelerator option to choose, for example T4 GPU, L4 GPU, A100 GPU, H100 GPU, GPU, TPU, or CPU.",
                },
                "apply": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true, press Save/Apply and confirm restart dialogs. If false, only select the option in the dialog.",
                },
            },
            "additionalProperties": False,
        },
    },
    "resource_usage_snapshot": {
        "description": "Captures CPU, memory, disk, and GPU usage through Colab Terminal and optionally appends it to a JSONL file.",
        "parameters": {
            "type": "object",
            "properties": {
                "savePath": {
                    "type": "string",
                    "default": "/content/colab_mcp_usage.jsonl",
                    "description": "Runtime path to append the JSON usage snapshot to. Set to an empty string to avoid saving.",
                },
            },
            "additionalProperties": False,
        },
    },
    "run_shell_command": {
        "description": "Runs a short shell command through Colab Terminal and waits for completion. Do NOT use this for training or long jobs; use start_background_command plus watch_background_command instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run in the Colab runtime.",
                },
                "timeoutSeconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 600,
                    "description": "Maximum seconds to wait before killing the command.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory inside the Colab runtime.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    "start_background_command": {
        "description": "Preferred tool for training and long jobs. Starts a background shell command through Colab Terminal, writes stdout/stderr to a log file, and records status for watch_background_command.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to start in the background.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional stable name for the background command.",
                },
                "logPath": {
                    "type": "string",
                    "description": "Optional runtime log path. Defaults to /content/colab_mcp_logs/<name>.log.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory inside the Colab runtime.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    "check_background_command": {
        "description": "Checks through Colab Terminal the status of a background command started by start_background_command.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Background command name.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "tail_file": {
        "description": "Returns through Colab Terminal the last lines or bytes from a file in the Colab runtime, useful for background command logs.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Runtime file path to read.",
                },
                "lines": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 80,
                    "description": "Number of trailing lines to return.",
                },
                "maxBytes": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 20000,
                    "description": "Maximum trailing bytes to read before splitting into lines.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "stop_background_command": {
        "description": "Stops through Colab Terminal a background command started by start_background_command.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Background command name.",
                },
                "signal": {
                    "type": "integer",
                    "default": 15,
                    "description": "POSIX signal number to send. Defaults to SIGTERM.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "import_notebook": {
        "description": "Imports cells from a local .ipynb file into the current Colab notebook.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local path to the .ipynb file on the machine running Codex/MCP.",
                },
                "cellIndex": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "description": "Optional insertion index. Defaults to the end of the current notebook.",
                },
                "replaceExisting": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, delete current notebook cells before importing.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "export_notebook": {
        "description": "Exports the current Colab notebook cells to a local .ipynb file on the machine running Codex/MCP.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local destination path for the .ipynb file on the machine running Codex/MCP.",
                },
                "includeOutputs": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include code cell outputs in the exported notebook.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "download_notebook": {
        "description": "Alias for export_notebook. Downloads the current Colab notebook cells to a local .ipynb file on the machine running Codex/MCP.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "includeOutputs": {"type": "boolean", "default": True},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "upload_notebook": {
        "description": "Alias for import_notebook. Uploads cells from a local .ipynb file into the current Colab notebook.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "cellIndex": {"type": "integer", "minimum": 0},
                "replaceExisting": {"type": "boolean", "default": False},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "run_code_cells": {
        "description": "Runs a list of code cells in order and returns per-cell outputs.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIds": {"type": "array", "items": {"type": "string"}},
                "stopOnError": {"type": "boolean", "default": True},
                "includeOutputs": {"type": "boolean", "default": True},
                "timeoutSeconds": {"type": "number", "minimum": 0},
            },
            "required": ["cellIds"],
            "additionalProperties": False,
        },
    },
    "run_cell_range": {
        "description": "Runs code cells in an inclusive index range.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIndexStart": {"type": "integer", "minimum": 0},
                "cellIndexEnd": {"type": "integer", "minimum": 0},
                "stopOnError": {"type": "boolean", "default": True},
                "includeOutputs": {"type": "boolean", "default": True},
                "timeoutSeconds": {"type": "number", "minimum": 0},
            },
            "required": ["cellIndexStart", "cellIndexEnd"],
            "additionalProperties": False,
        },
    },
    "run_all_cells": {
        "description": "Runs all code cells in notebook order.",
        "parameters": {
            "type": "object",
            "properties": {
                "stopOnError": {"type": "boolean", "default": True},
                "includeOutputs": {"type": "boolean", "default": True},
                "timeoutSeconds": {"type": "number", "minimum": 0},
            },
            "additionalProperties": False,
        },
    },
    "cancel_queued_cells": {
        "description": "Best-effort queued cell cancellation. Returns no-op status when Colab queue controls are not exposed through MCP.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIds": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
    "get_cell": {
        "description": "Returns a single cell by cell ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellId": {"type": "string"},
                "includeOutputs": {"type": "boolean", "default": False},
            },
            "required": ["cellId"],
            "additionalProperties": False,
        },
    },
    "find_cells": {
        "description": "Finds cells by source or output text.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "regex": {"type": "boolean", "default": False},
                "cellType": {"type": "string"},
                "includeOutputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    "replace_cells": {
        "description": "Bulk inserts cells and optionally deletes existing cells first.",
        "parameters": {
            "type": "object",
            "properties": {
                "cells": {"type": "array", "items": {"type": "object"}},
                "replaceExisting": {"type": "boolean", "default": False},
                "cellIndex": {"type": "integer", "minimum": 0},
            },
            "required": ["cells"],
            "additionalProperties": False,
        },
    },
    "patch_cell": {
        "description": "Partially updates a cell via search-and-replace (target and replacement) or full source/content overwrite.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellId": {
                    "type": "string",
                    "description": "The ID of the cell to update.",
                },
                "target": {
                    "type": "string",
                    "description": "The exact string to find and replace in the cell.",
                },
                "replacement": {
                    "type": "string",
                    "description": "The string to replace the target with (defaults to empty string).",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of occurrences to replace. Defaults to -1 (replace all).",
                },
                "source": {
                    "type": "string",
                    "description": "Full cell source content (for backward compatibility).",
                },
                "content": {
                    "type": "string",
                    "description": "Full cell content (alias for source).",
                },
            },
            "required": ["cellId"],
            "additionalProperties": False,
        },
    },
    "get_cell_status": {
        "description": "Returns best-effort execution status for selected cells or all cells.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIds": {"type": "array", "items": {"type": "string"}},
                "includeOutputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    "wait_for_cells": {
        "description": "Polls cell status until every selected cell reaches one of the target states.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIds": {"type": "array", "items": {"type": "string"}},
                "targetStates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["success", "error"],
                },
                "timeoutSeconds": {"type": "number", "minimum": 0, "default": 60},
                "pollIntervalSeconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "default": 0.5,
                },
            },
            "required": ["cellIds"],
            "additionalProperties": False,
        },
    },
    "read_cell_outputs": {
        "description": "Returns outputs for selected cells, tailing the latest lines (default: 20) to prevent oversized logs.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIds": {"type": "array", "items": {"type": "string"}},
                "lines": {
                    "type": "integer",
                    "default": 20,
                    "description": "Number of latest lines of output to return per cell. Defaults to 20. Pass <= 0 to return all lines.",
                },
                "maxBytes": {"type": "integer", "minimum": 1, "default": 20000},
                "includeRaw": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to include raw Jupyter outputs array. Defaults to False to save context tokens.",
                },
                "normalize": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to include normalized output chunks. Defaults to False.",
                },
            },
            "required": ["cellIds"],
            "additionalProperties": False,
        },
    },
    "watch_cell_outputs": {
        "description": "Polls selected cell outputs for a short period and returns the latest outputs.",
        "parameters": {
            "type": "object",
            "properties": {
                "cellIds": {"type": "array", "items": {"type": "string"}},
                "lines": {
                    "type": "integer",
                    "default": 20,
                    "description": "Number of latest lines of output to return per cell. Defaults to 20. Pass <= 0 to return all lines.",
                },
                "timeoutSeconds": {"type": "number", "minimum": 0, "default": 10},
                "pollIntervalSeconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "default": 0.5,
                },
                "maxBytes": {"type": "integer", "minimum": 1, "default": 20000},
                "includeRaw": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to include raw Jupyter outputs array. Defaults to False to save context tokens.",
                },
                "normalize": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to include normalized output chunks. Defaults to False.",
                },
            },
            "required": ["cellIds"],
            "additionalProperties": False,
        },
    },
    "get_env_vars": {
        "description": "Reads environment variables from the Colab runtime with secret redaction in text output.",
        "parameters": {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}},
                "redactPatterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["KEY", "TOKEN", "SECRET", "PASSWORD"],
                },
            },
            "additionalProperties": False,
        },
    },
    "unset_env_vars": {
        "description": "Unsets environment variables in the current Colab Python runtime.",
        "parameters": {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["names"],
            "additionalProperties": False,
        },
    },
    "load_env_file": {
        "description": "Loads a local .env file and applies it to the current Colab Python runtime.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "persist": {"type": "boolean", "default": False},
                "markerName": {"type": "string", "default": "default"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "list_background_commands": {
        "description": "Lists through Colab Terminal background commands registered by start_background_command.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "watch_background_command": {
        "description": "Polls through Colab Terminal a background command and returns status plus recent log tail.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "lines": {"type": "integer", "minimum": 1, "default": 80},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "upload_file": {
        "description": "Writes a base64 encoded file into the Colab runtime through Colab's browser file upload API without consuming a notebook cell execution slot.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "contentBase64": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
                "makeParents": {"type": "boolean", "default": True},
            },
            "required": ["path", "contentBase64"],
            "additionalProperties": False,
        },
    },
    "upload_local_file": {
        "description": "Uploads a file from the local machine running this MCP server to the Colab runtime. Prefer this over upload_file when the source is a local path.",
        "parameters": {
            "type": "object",
            "properties": {
                "localPath": {
                    "type": "string",
                    "description": "Absolute or relative path on the local machine running the MCP server.",
                },
                "path": {
                    "type": "string",
                    "description": "Destination path in the Colab runtime, for example /content/train.py.",
                },
                "overwrite": {"type": "boolean", "default": False},
                "makeParents": {"type": "boolean", "default": True},
            },
            "required": ["localPath", "path"],
            "additionalProperties": False,
        },
    },
    "download_file": {
        "description": "Reads a file from the Colab runtime through Colab's /files download API and returns base64 content without consuming a notebook cell execution slot.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "maxBytes": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "download_file_to_local": {
        "description": "Downloads a file from the Colab runtime to a local path on the machine running this MCP server. Prefer this over download_file when the target is a local file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Source path in the Colab runtime, for example /content/model.pt.",
                },
                "localPath": {
                    "type": "string",
                    "description": "Destination path on the local machine running the MCP server.",
                },
                "overwrite": {"type": "boolean", "default": False},
                "makeParents": {"type": "boolean", "default": True},
            },
            "required": ["path", "localPath"],
            "additionalProperties": False,
        },
    },
    "upload_file_chunk": {
        "description": "Appends or writes one base64 chunk to a temporary upload file in the Colab runtime through Colab's browser file upload API.",
        "parameters": {
            "type": "object",
            "properties": {
                "uploadId": {"type": "string"},
                "path": {"type": "string"},
                "chunkBase64": {"type": "string"},
                "chunkIndex": {"type": "integer", "minimum": 0},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["uploadId", "path", "chunkBase64", "chunkIndex"],
            "additionalProperties": False,
        },
    },
    "complete_upload": {
        "description": "Moves a temporary chunked upload into its final runtime path through Colab's browser file upload API.",
        "parameters": {
            "type": "object",
            "properties": {
                "uploadId": {"type": "string"},
                "path": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["uploadId", "path"],
            "additionalProperties": False,
        },
    },
    "download_file_chunk": {
        "description": "Reads one base64 chunk from a file in the Colab runtime through Colab's /files download API.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "maxBytes": {"type": "integer", "minimum": 1, "default": 1048576},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "stat_file": {
        "description": "Stats a path in the Colab runtime through Colab's runtime file APIs.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "list_files": {
        "description": "Lists files in the Colab runtime through Colab's contents API.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "recursive": {"type": "boolean", "default": False},
                "maxEntries": {"type": "integer", "minimum": 1, "default": 1000},
            },
            "additionalProperties": False,
        },
    },
    "delete_file": {
        "description": "Deletes a file or directory in the Colab runtime through Colab's contents API.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "make_directory": {
        "description": "Creates a directory in the Colab runtime through Colab's contents API.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "parents": {"type": "boolean", "default": True},
                "existOk": {"type": "boolean", "default": True},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "sample_gpu_usage": {
        "description": "Samples GPU utilization with nvidia-smi through Colab Terminal and optionally writes JSONL.",
        "parameters": {
            "type": "object",
            "properties": {
                "intervalSeconds": {"type": "number", "minimum": 0, "default": 1.0},
                "count": {"type": "integer", "minimum": 1, "default": 1},
                "savePath": {
                    "type": "string",
                    "default": "/content/colab_mcp_gpu.jsonl",
                },
            },
            "additionalProperties": False,
        },
    },
    "start_gpu_monitor": {
        "description": "Starts a lightweight nvidia-smi background sampler through Colab Terminal that writes timestamped JSONL samples.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "gpu"},
                "intervalSeconds": {"type": "number", "minimum": 0.1, "default": 1.0},
                "savePath": {
                    "type": "string",
                    "default": "/content/colab_mcp_gpu.jsonl",
                },
            },
            "additionalProperties": False,
        },
    },
    "stop_gpu_monitor": {
        "description": "Stops a GPU monitor started by start_gpu_monitor.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "default": "gpu"}},
            "additionalProperties": False,
        },
    },
    "read_gpu_monitor": {
        "description": "Reads recent JSONL samples from a GPU monitor file.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "gpu"},
                "path": {"type": "string"},
                "lines": {"type": "integer", "minimum": 1, "default": 100},
            },
            "additionalProperties": False,
        },
    },
    "get_connection_info": {
        "description": "Returns the local Colab MCP WebSocket connection details and scratch URL for manually connecting a browser.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


class ColabTransport(ClientTransport):
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss

    @contextlib.asynccontextmanager
    async def connect_session(self, **session_kwargs) -> AsyncIterator[ClientSession]:
        async with ClientSession(
            self.wss.read_stream, self.wss.write_stream, **session_kwargs
        ) as session:
            yield session

    def __repr__(self) -> str:
        return "<ColabSessionProxyTransport>"


class ColabProxyClient:
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss
        self.stubbed_mcp_client = Client(FastMCP())
        self.proxy_mcp_client: Client | None = None
        self._exit_stack = AsyncExitStack()
        self._start_task = None

    def is_connected(self):
        return self.wss.connection_live.is_set() and self.proxy_mcp_client is not None

    async def await_proxy_connection(self, timeout: float | None = None):
        wait_timeout = timeout if timeout is not None else UI_CONNECTION_TIMEOUT
        with contextlib.suppress(asyncio.TimeoutError):
            # wait for the connection to be live and for the proxy client to fully initialize
            connection_tasks = asyncio.gather(
                self.wss.connection_live.wait(), self._start_task
            )
            await asyncio.wait_for(
                connection_tasks,
                timeout=wait_timeout,
            )

    def client_factory(self):
        if self.is_connected():
            return self.proxy_mcp_client
        # return a client mapped to a stubbed mcp server if there is no session proxy
        return self.stubbed_mcp_client

    async def _start_proxy_client(self):
        # blocks until a websocket connection is made successfully
        self.proxy_mcp_client = await self._exit_stack.enter_async_context(
            Client(ColabTransport(self.wss))
        )

    async def __aenter__(self):
        self._start_task = asyncio.create_task(self._start_proxy_client())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._start_task:
            self._start_task.cancel()
        await self._exit_stack.aclose()


class StaticColabProxyTool(Tool):
    _proxy_client: ColabProxyClient = PrivateAttr()
    _upstream_name: str = PrivateAttr()

    def __init__(
        self,
        *,
        proxy_client: ColabProxyClient,
        upstream_name: str,
        description: str,
        parameters: dict,
    ):
        super().__init__(
            name=upstream_name,
            description=description,
            parameters=parameters,
            output_schema=None,
        )
        self._proxy_client = proxy_client
        self._upstream_name = upstream_name

    async def run(self, arguments: dict) -> ToolResult:
        if not self._proxy_client.is_connected():
            await self._proxy_client.await_proxy_connection()
        if (
            not self._proxy_client.is_connected()
            or self._proxy_client.proxy_mcp_client is None
        ):
            raise RuntimeError(
                "Colab UI is not connected. Call open_colab_browser_connection first."
            )

        result = await self._proxy_client.proxy_mcp_client.call_tool_mcp(
            self._upstream_name, arguments
        )
        structured_content = getattr(result, "structuredContent", None)
        if structured_content is None:
            structured_content = getattr(result, "structured_content", None)
        structured_content = dict(structured_content or {})
        warnings = structured_content.get("warnings") or []
        if structured_content.get("warning"):
            warnings = [*warnings, structured_content["warning"]]
        if structured_content.get("error") and not warnings:
            warnings = [str(structured_content["error"])]
        failed = structured_content.get("ok") is False or bool(
            structured_content.get("error")
        )
        status = "warning" if warnings or failed else "ok"
        return ToolResult(
            content=result.content,
            structured_content={
                **structured_content,
                "ok": status == "ok",
                "status": status,
                "warnings": warnings,
            },
            meta=getattr(result, "meta", None),
        )


class ColabRuntimeManagementTool(Tool):
    _proxy_client: ColabProxyClient = PrivateAttr()

    def __init__(
        self,
        *,
        proxy_client: ColabProxyClient,
        name: str,
        description: str,
        parameters: dict,
    ):
        super().__init__(
            name=name,
            description=description,
            parameters=parameters,
            output_schema=None,
        )
        self._proxy_client = proxy_client

    async def _call_colab(self, name: str, arguments: dict):
        if not self._proxy_client.is_connected():
            await self._proxy_client.await_proxy_connection()
        if (
            not self._proxy_client.is_connected()
            or self._proxy_client.proxy_mcp_client is None
        ):
            raise RuntimeError(
                "Colab UI is not connected. Call open_colab_browser_connection first."
            )
        last_exc = None
        for attempt in range(3):
            try:
                return await self._proxy_client.proxy_mcp_client.call_tool_mcp(
                    name, arguments
                )
            except Exception as exc:
                last_exc = exc
                if attempt == 2:
                    break
                await asyncio.sleep(0.25 * (attempt + 1))
        raise last_exc

    @staticmethod
    def _result_text(result) -> str:
        return result.content[0].text

    @staticmethod
    def _cell_source(cell: dict) -> str:
        source = cell.get("source", "")
        if isinstance(source, list):
            return "".join(source)
        return source or ""

    @staticmethod
    def _cell_status(cell: dict, index: int) -> dict:
        outputs = cell.get("outputs") or []
        has_error = any(output.get("output_type") == "error" for output in outputs)
        execution_count = cell.get("execution_count")
        if has_error:
            state = "error"
        elif execution_count is not None:
            state = "success"
        else:
            state = "idle"
        last_output_text = ""
        if outputs:
            last = outputs[-1]
            text = last.get("text") or last.get("evalue") or ""
            if isinstance(text, list):
                text = "".join(text)
            last_output_text = str(text)
        return {
            "cellId": cell.get("id"),
            "index": index,
            "cellType": cell.get("cell_type"),
            "state": state,
            "busy": False,
            "queued": False,
            "executionCount": execution_count,
            "hasError": has_error,
            "lastOutputText": last_output_text,
            "outputCount": len(outputs),
            "outputs": outputs,
        }

    @staticmethod
    def _redacted_env(values: dict, patterns: list[str]) -> dict:
        redacted = {}
        upper_patterns = [pattern.upper() for pattern in patterns]
        for key, value in values.items():
            if any(pattern in key.upper() for pattern in upper_patterns):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = value
        return redacted

    @staticmethod
    def _normalize_outputs(
        outputs: list[dict], *, observed_at: float | None = None
    ) -> list[dict]:
        import time

        timestamp = observed_at if observed_at is not None else time.time()
        chunks = []
        for index, output in enumerate(outputs):
            output_type = output.get("output_type", "unknown")
            chunk = {
                "index": index,
                "outputType": output_type,
                "timestamp": timestamp,
                "name": output.get("name"),
                "text": "",
                "data": output.get("data"),
                "metadata": output.get("metadata"),
                "ename": output.get("ename"),
                "evalue": output.get("evalue"),
                "traceback": output.get("traceback"),
            }
            if "text" in output:
                text = output.get("text")
                chunk["text"] = "".join(text) if isinstance(text, list) else str(text)
            elif output_type == "error":
                traceback = output.get("traceback") or []
                chunk["text"] = (
                    "\n".join(traceback) if traceback else str(output.get("evalue", ""))
                )
            elif "data" in output:
                data = output.get("data") or {}
                text = data.get("text/plain", "")
                chunk["text"] = "".join(text) if isinstance(text, list) else str(text)
            chunks.append(chunk)
        return chunks

    @staticmethod
    def _tail_chunks_and_text(
        chunks: list[dict], lines: int | None
    ) -> tuple[list[dict], str, int]:
        """
        Tails chunks so that total lines across all chunks is at most `lines`.
        Returns (tailed_chunks, joined_tail_text, total_lines_before_tail).
        """
        all_chunk_lines = [chunk.get("text", "").splitlines() for chunk in chunks]
        total_lines = sum(len(cl) for cl in all_chunk_lines)
        if lines is None or lines <= 0 or total_lines <= lines:
            joined = "\n".join(line for cl in all_chunk_lines for line in cl)
            return chunks, joined, total_lines

        remaining = lines
        tailed = []
        for chunk, cl in zip(reversed(chunks), reversed(all_chunk_lines)):
            if remaining <= 0:
                break
            if len(cl) <= remaining:
                tailed.append(chunk)
                remaining -= len(cl)
            else:
                tailed_cl = cl[-remaining:]
                new_chunk = dict(chunk)
                new_chunk["text"] = "\n".join(tailed_cl)
                tailed.append(new_chunk)
                remaining = 0
                break

        tailed.reverse()
        joined = "\n".join(
            line for chunk in tailed for line in chunk.get("text", "").splitlines()
        )
        return tailed, joined, total_lines

    @staticmethod
    def _tail_raw_outputs(outputs: list[dict], lines: int | None) -> list[dict]:
        """
        Tails raw jupyter outputs to keep at most `lines` lines across all outputs.
        """
        if not outputs or lines is None or lines <= 0:
            return outputs

        def get_lines(op):
            if "text" in op:
                t = op["text"]
                return t if isinstance(t, list) else str(t).splitlines(keepends=True)
            if op.get("output_type") == "error":
                tb = op.get("traceback") or []
                if tb:
                    return tb
                return [str(op.get("evalue", "")) + "\n"]
            if (
                "data" in op
                and isinstance(op["data"], dict)
                and "text/plain" in op["data"]
            ):
                tp = op["data"]["text/plain"]
                return tp if isinstance(tp, list) else str(tp).splitlines(keepends=True)
            return ["\n"]

        op_lines = [get_lines(op) for op in outputs]
        total = sum(len(lines_item) for lines_item in op_lines)
        if total <= lines:
            return outputs

        remaining = lines
        tailed = []
        for op, lines_item in zip(reversed(outputs), reversed(op_lines)):
            if remaining <= 0:
                break
            if len(lines_item) <= remaining:
                tailed.append(op)
                remaining -= len(lines_item)
            else:
                new_op = dict(op)
                tailed_l = lines_item[-remaining:]
                if "text" in new_op:
                    new_op["text"] = (
                        "".join(tailed_l)
                        if isinstance(new_op["text"], str)
                        else tailed_l
                    )
                elif new_op.get("output_type") == "error":
                    new_op["traceback"] = tailed_l
                elif (
                    "data" in new_op
                    and isinstance(new_op["data"], dict)
                    and "text/plain" in new_op["data"]
                ):
                    new_data = dict(new_op["data"])
                    new_data["text/plain"] = (
                        "".join(tailed_l)
                        if isinstance(new_data["text/plain"], str)
                        else tailed_l
                    )
                    new_op["data"] = new_data
                tailed.append(new_op)
                remaining = 0
                break

        tailed.reverse()
        return tailed

    @staticmethod
    def _notebook_document(cells: list[dict], *, name: str | None = None) -> dict:
        return {
            "cells": cells,
            "metadata": {
                "colab": {
                    "name": name,
                    "provenance": [],
                },
                "kernelspec": {
                    "display_name": "Python 3",
                    "name": "python3",
                },
                "language_info": {
                    "name": "python",
                    "pygments_lexer": "ipython3",
                },
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }

    @staticmethod
    def _structured(
        content: dict | None = None,
        *,
        ok: bool = True,
        status: str = "ok",
        warnings: list[str] | None = None,
        **extra,
    ) -> dict:
        result = {}
        if content:
            result.update(content)
        result.update(extra)
        final_warnings = list(
            warnings if warnings is not None else result.get("warnings") or []
        )
        if result.get("warning"):
            final_warnings.append(str(result["warning"]))
        if result.get("error") and not final_warnings:
            final_warnings.append(str(result["error"]))
        failed = result.get("ok") is False or bool(result.get("error"))
        if result.get("error") or result.get("status") == "error":
            result["ok"] = False
            result["status"] = "error"
        elif final_warnings or failed:
            result["ok"] = False
            result["status"] = "warning"
        else:
            result.setdefault("ok", ok)
            result.setdefault("status", status)
        result["warnings"] = final_warnings
        return result

    @staticmethod
    def _parse_nvidia_smi_gpu_csv(stdout: str) -> list[dict]:
        gpus = []
        for line in stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 9:
                continue
            gpus.append(
                {
                    "gpuIndex": parts[0],
                    "name": parts[1],
                    "memoryTotalMiB": parts[2],
                    "memoryUsedMiB": parts[3],
                    "memoryFreeMiB": parts[4],
                    "gpuUtilizationPercent": parts[5],
                    "memoryUtilizationPercent": parts[6],
                    "temperatureC": parts[7],
                    "powerDrawW": parts[8],
                }
            )
        return gpus

    async def _append_and_run_code(self, code: str, *, delete_after: bool = False):
        cells = await self._call_colab("get_cells", {"includeOutputs": False})
        cell_count = len(json.loads(self._result_text(cells))["cells"])
        added = await self._call_colab(
            "add_code_cell",
            {"cellIndex": cell_count, "language": "python", "code": code},
        )
        cell_id = json.loads(self._result_text(added))["newCellId"]
        result = await self._call_colab("run_code_cell", {"cellId": cell_id})
        if delete_after:
            await self._call_colab("delete_cell", {"cellId": cell_id})
        return cell_id, result

    async def _run_terminal_command(
        self,
        command: str,
        *,
        timeout_seconds: int = 600,
        marker: str | None = None,
        result_path: str | None = None,
    ) -> dict:
        marker = marker or f"CM{uuid.uuid4().hex}"
        async with _terminal_command_lock():
            last_exc = None
            for attempt in range(3):
                try:
                    output = await asyncio.to_thread(
                        _run_direct_terminal_command,
                        command,
                        marker=marker,
                        timeout_seconds=max(1, int(timeout_seconds)),
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.25 * (attempt + 1))
            else:
                raise last_exc or RuntimeError("Terminal command failed.")
        output_for_marker = _strip_ansi_sequences(output)
        if result_path and f"{marker}RESULT{marker}" in output_for_marker:
            result = json.loads(_read_runtime_text_file(result_path))
            result.setdefault("executionBackend", "colab-terminal")
            return result
        chunk_matches = re.findall(
            re.escape(marker) + r"(\d+):([A-Za-z0-9+/=]*)" + re.escape(marker),
            output_for_marker,
            re.DOTALL,
        )
        if chunk_matches:
            chunks_by_index = {}
            for index, chunk in chunk_matches:
                chunks_by_index[int(index)] = chunk
            payload = "".join(chunk for _, chunk in sorted(chunks_by_index.items()))
            result = json.loads(base64.b64decode(payload).decode("utf-8"))
            result.setdefault("executionBackend", "colab-terminal")
            return result
        match = re.search(
            re.escape(marker) + r"([A-Za-z0-9+/=]+)" + re.escape(marker),
            output_for_marker,
            re.DOTALL,
        )
        if not match:
            raise TimeoutError(
                f"Timed out waiting for terminal result marker after {timeout_seconds} seconds."
            )
        payload = base64.b64decode(match.group(1)).decode("utf-8")
        result = json.loads(payload)
        result.setdefault("executionBackend", "colab-terminal")
        return result

    async def _run_terminal_python(
        self, code: str, *, timeout_seconds: int = 600
    ) -> dict:
        marker = f"CM{uuid.uuid4().hex}"
        result_path = f"/content/.colab_mcp_results/{marker}.json"
        code = (
            "import os\n"
            "os.makedirs('/content/.colab_mcp_results', exist_ok=True)\n"
            f"os.environ['COLAB_MCP_RESULT_MARKER'] = {marker!r}\n"
            f"os.environ['COLAB_MCP_RESULT_PATH'] = {result_path!r}\n" + code
        )
        encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
        command = (
            "python3 -c "
            + "'import base64;exec(base64.b64decode(\""
            + encoded_code
            + '").decode("utf-8"))\''
        )
        return await self._run_terminal_command(
            command,
            timeout_seconds=timeout_seconds,
            marker=marker,
            result_path=result_path,
        )

    @staticmethod
    def _terminal_tool_result(result: dict) -> ToolResult:
        text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        warnings = list(result.get("warnings", []))
        if result.get("error") and not warnings:
            warnings.append(str(result["error"]))
        failed = (
            result.get("ok") is False
            or bool(result.get("error"))
            or result.get("timed_out") is True
            or (
                isinstance(result.get("returncode"), int)
                and result.get("returncode") != 0
            )
        )
        status = "error" if failed else "warning" if warnings else "ok"
        return ToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content={
                **result,
                "ok": status == "ok",
                "status": status,
                "warnings": warnings,
            },
        )

    @staticmethod
    def _runtime_tool_result(result: dict) -> ToolResult:
        result.setdefault("executionBackend", "colab-file-api")
        text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        warnings = list(result.get("warnings", []))
        if result.get("error") and not warnings:
            warnings.append(str(result["error"]))
        failed = result.get("ok") is False or bool(result.get("error"))
        status = "error" if failed else "warning" if warnings else "ok"
        return ToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content={
                **result,
                "ok": status == "ok",
                "status": status,
                "warnings": warnings,
            },
        )

    async def run(self, arguments: dict) -> ToolResult:
        logging.info(
            "colab_mcp tool=%s argument_keys=%s",
            self.name,
            sorted(arguments.keys()),
        )
        if self.name == "get_connection_info":
            info = dict(self._proxy_client.wss.connection_info())
            info["connected"] = self._proxy_client.is_connected()
            text = json.dumps(info, ensure_ascii=False)
            return ToolResult(
                content=[TextContent(type="text", text=text)],
                structured_content=info,
            )
        if self.name == "set_env_vars":
            variables = arguments["variables"]
            if not isinstance(variables, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in variables.items()
            ):
                raise ValueError(
                    "variables must be an object of string keys and values"
                )

            persist = bool(arguments.get("persist", False))
            marker_name = arguments.get("markerName") or "default"
            cell_index = arguments.get("cellIndex")
            assignments = json.dumps(variables, ensure_ascii=True)
            code = (
                f"{ENV_SETUP_MARKER_PREFIX} {marker_name}\n"
                "import os\n"
                f"_colab_mcp_env = {assignments}\n"
                "os.environ.update(_colab_mcp_env)\n"
                "print('Set environment variables:', ', '.join(sorted(_colab_mcp_env)))\n"
            )
            if cell_index is None:
                cells = await self._call_colab("get_cells", {"includeOutputs": False})
                cell_index = len(json.loads(self._result_text(cells))["cells"])
            added = await self._call_colab(
                "add_code_cell",
                {"cellIndex": cell_index, "language": "python", "code": code},
            )
            cell_id = json.loads(self._result_text(added))["newCellId"]
            result = await self._call_colab("run_code_cell", {"cellId": cell_id})
            if not persist:
                await self._call_colab("delete_cell", {"cellId": cell_id})
            return ToolResult(
                content=result.content,
                structured_content={
                    "cellId": cell_id,
                    "markerName": marker_name,
                    "persisted": persist,
                    "variables": sorted(variables),
                },
            )

        if self.name == "rerun_env_setup_cells":
            marker_name = arguments.get("markerName")
            include_outputs = bool(arguments.get("includeOutputs", True))
            cells_result = await self._call_colab(
                "get_cells", {"includeOutputs": False}
            )
            cells = json.loads(self._result_text(cells_result))["cells"]
            matched = []
            for cell in cells:
                source = self._cell_source(cell)
                if not source.startswith(ENV_SETUP_MARKER_PREFIX):
                    continue
                first_line = source.splitlines()[0].strip()
                current_marker = first_line.removeprefix(
                    ENV_SETUP_MARKER_PREFIX
                ).strip()
                if marker_name and current_marker != marker_name:
                    continue
                matched.append((cell["id"], current_marker))

            outputs = []
            for cell_id, current_marker in matched:
                result = await self._call_colab("run_code_cell", {"cellId": cell_id})
                if include_outputs:
                    outputs.append(
                        {
                            "cellId": cell_id,
                            "markerName": current_marker,
                            "outputs": json.loads(self._result_text(result)).get(
                                "outputs", []
                            ),
                        }
                    )
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Reran {len(matched)} Colab MCP env setup cell(s).",
                    )
                ],
                structured_content={"count": len(matched), "cells": outputs},
            )

        if self.name == "load_env_file":
            path = Path(arguments["path"]).expanduser()
            if not path.exists():
                raise FileNotFoundError(f".env file not found: {path}")
            variables = {}
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    variables[key] = value
            arguments = {
                "variables": variables,
                "persist": bool(arguments.get("persist", False)),
                "markerName": arguments.get("markerName") or "default",
            }
            persist = bool(arguments.get("persist", False))
            marker_name = arguments.get("markerName") or "default"
            assignments = json.dumps(variables, ensure_ascii=True)
            code = (
                f"{ENV_SETUP_MARKER_PREFIX} {marker_name}\n"
                "import os\n"
                f"_colab_mcp_env = {assignments}\n"
                "os.environ.update(_colab_mcp_env)\n"
                "print('Set environment variables:', ', '.join(sorted(_colab_mcp_env)))\n"
            )
            cells = await self._call_colab("get_cells", {"includeOutputs": False})
            cell_index = len(json.loads(self._result_text(cells))["cells"])
            added = await self._call_colab(
                "add_code_cell",
                {"cellIndex": cell_index, "language": "python", "code": code},
            )
            cell_id = json.loads(self._result_text(added))["newCellId"]
            result = await self._call_colab("run_code_cell", {"cellId": cell_id})
            if not persist:
                await self._call_colab("delete_cell", {"cellId": cell_id})
            return ToolResult(
                content=result.content,
                structured_content={
                    "cellId": cell_id,
                    "markerName": marker_name,
                    "persisted": persist,
                    "variables": sorted(variables),
                    "sourcePath": str(path),
                },
            )

        if self.name == "get_env_vars":
            names = arguments.get("names")
            patterns = arguments.get(
                "redactPatterns", ["KEY", "TOKEN", "SECRET", "PASSWORD"]
            )
            code = (
                "import json, os\n"
                f"names = {names!r}\n"
                f"patterns = {patterns!r}\n"
                "if names:\n"
                "    values = {name: os.environ.get(name) for name in names}\n"
                "else:\n"
                "    values = dict(os.environ)\n"
                "upper_patterns = [p.upper() for p in patterns]\n"
                "redacted = {k: ('<redacted>' if any(p in k.upper() for p in upper_patterns) else v) for k, v in values.items()}\n"
                "print(json.dumps({'values': values, 'redacted': redacted}, ensure_ascii=False, indent=2, sort_keys=True))\n"
            )
            cell_id, result = await self._append_and_run_code(code, delete_after=True)
            return ToolResult(
                content=result.content,
                structured_content={"cellId": cell_id, "deletedCell": True},
            )

        if self.name == "unset_env_vars":
            names = arguments["names"]
            code = (
                "import json, os\n"
                f"names = {names!r}\n"
                "removed = {}\n"
                "for name in names:\n"
                "    removed[name] = os.environ.pop(name, None) is not None\n"
                "print(json.dumps({'removed': removed}, indent=2, sort_keys=True))\n"
            )
            cell_id, result = await self._append_and_run_code(code, delete_after=True)
            return ToolResult(
                content=result.content,
                structured_content={"cellId": cell_id, "deletedCell": True},
            )

        if self.name in {"run_code_cells", "run_cell_range", "run_all_cells"}:
            if self.name == "run_code_cells":
                cell_ids = list(arguments["cellIds"])
                if not cell_ids:
                    raise ValueError("cellIds must not be empty")
            else:
                cells_result = await self._call_colab(
                    "get_cells", {"includeOutputs": False}
                )
                cells = json.loads(self._result_text(cells_result))["cells"]
                if self.name == "run_cell_range":
                    start = int(arguments["cellIndexStart"])
                    end = int(arguments["cellIndexEnd"])
                    if start > end:
                        raise ValueError("cellIndexStart must be <= cellIndexEnd")
                    if start >= len(cells):
                        raise ValueError(f"cellIndexStart out of range: {start}")
                    selected = cells[start : end + 1]
                else:
                    selected = cells
                cell_ids = [
                    cell["id"]
                    for cell in selected
                    if cell.get("cell_type") == "code" and cell.get("id")
                ]
            stop_on_error = bool(arguments.get("stopOnError", True))
            include_outputs = bool(arguments.get("includeOutputs", True))
            timeout_seconds = arguments.get("timeoutSeconds")
            results = []
            for cell_id in cell_ids:
                import time

                started_at = time.time()
                timed_out = False
                try:
                    call = self._call_colab("run_code_cell", {"cellId": cell_id})
                    if timeout_seconds is not None:
                        result = await asyncio.wait_for(
                            call, timeout=float(timeout_seconds)
                        )
                    else:
                        result = await call
                    payload = json.loads(self._result_text(result))
                except asyncio.TimeoutError:
                    payload = {"outputs": []}
                    timed_out = True
                ended_at = time.time()
                outputs = payload.get("outputs", [])
                has_error = any(
                    output.get("output_type") == "error" for output in outputs
                )
                error = next(
                    (
                        output
                        for output in outputs
                        if output.get("output_type") == "error"
                    ),
                    None,
                )
                entry = {
                    "cellId": cell_id,
                    "startedAt": started_at,
                    "endedAt": ended_at,
                    "durationSeconds": ended_at - started_at,
                    "status": "timeout"
                    if timed_out
                    else ("error" if has_error else "success"),
                    "hasError": has_error,
                    "timedOut": timed_out,
                    "executionCount": payload.get("execution_count"),
                    "error": error,
                }
                if include_outputs:
                    entry["outputs"] = outputs
                results.append(entry)
                if (has_error or timed_out) and stop_on_error:
                    break
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"cellCount": len(results), "cells": results},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                ],
                structured_content={"cellCount": len(results), "cells": results},
            )

        if self.name == "cancel_queued_cells":
            cell_ids = arguments.get("cellIds") or []
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "cancelled": [],
                                "requestedCellIds": cell_ids,
                                "status": "noop",
                                "warning": "Colab queued-cell cancellation is not exposed through the current MCP frontend tools.",
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                ],
                structured_content={
                    "ok": False,
                    "cancelled": [],
                    "requestedCellIds": cell_ids,
                    "status": "warning",
                    "warnings": [
                        "Colab queued-cell cancellation is not exposed through the current MCP frontend tools."
                    ],
                },
            )

        if self.name == "replace_cells":
            if arguments.get("replaceExisting", False):
                existing = await self._call_colab(
                    "get_cells", {"includeOutputs": False}
                )
                for cell in reversed(json.loads(self._result_text(existing))["cells"]):
                    await self._call_colab("delete_cell", {"cellId": cell["id"]})
            cell_index = arguments.get("cellIndex")
            if cell_index is None:
                cells_result = await self._call_colab(
                    "get_cells", {"includeOutputs": False}
                )
                cell_index = len(json.loads(self._result_text(cells_result))["cells"])
            inserted = []
            for source_cell in arguments["cells"]:
                cell_type = source_cell.get("cell_type") or source_cell.get("cellType")
                if cell_type not in {"code", "markdown", "text", None}:
                    raise ValueError(f"Unsupported cell type: {cell_type}")
                source = self._cell_source(source_cell)
                if not source:
                    source = source_cell.get("content", "")
                if cell_type == "code":
                    added = await self._call_colab(
                        "add_code_cell",
                        {
                            "cellIndex": cell_index,
                            "language": source_cell.get("language", "python"),
                            "code": source,
                        },
                    )
                else:
                    added = await self._call_colab(
                        "add_text_cell",
                        {"cellIndex": cell_index, "content": source},
                    )
                cell_id = json.loads(self._result_text(added))["newCellId"]
                inserted.append(
                    {"cellId": cell_id, "cellType": cell_type or "markdown"}
                )
                cell_index += 1
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps({"inserted": inserted}, ensure_ascii=False),
                    )
                ],
                structured_content={"inserted": inserted},
            )

        if self.name == "patch_cell":
            cell_id = arguments["cellId"]
            target = arguments.get("target")

            if target is not None:
                replacement = arguments.get("replacement", "")
                count = arguments.get("count", -1)

                cells_result = await self._call_colab(
                    "get_cells", {"includeOutputs": False}
                )
                cells = json.loads(self._result_text(cells_result)).get("cells", [])
                cell = next((c for c in cells if c.get("id") == cell_id), None)
                if cell is None:
                    raise ValueError(f"Cell not found: {cell_id}")

                current_content = self._cell_source(cell)
                if target not in current_content:
                    raise ValueError(f"Target text not found in cell '{cell_id}'")

                occurrences = current_content.count(target)
                if count is None or count < 0:
                    new_content = current_content.replace(target, replacement)
                    replaced_count = occurrences
                else:
                    new_content = current_content.replace(target, replacement, count)
                    replaced_count = min(count, occurrences)

                result = await self._call_colab(
                    "update_cell",
                    {"cellId": cell_id, "content": new_content},
                )
                return ToolResult(
                    content=result.content,
                    structured_content={
                        "cellId": cell_id,
                        "updated": True,
                        "patched": True,
                        "replacedCount": replaced_count,
                        "occurrences": occurrences,
                    },
                )

            content = arguments.get("source", arguments.get("content"))
            if content is None:
                raise ValueError(
                    "patch_cell requires target (and optional replacement) or source or content"
                )
            result = await self._call_colab(
                "update_cell",
                {"cellId": cell_id, "content": content},
            )
            structured_content = getattr(result, "structuredContent", None)
            if not isinstance(structured_content, (dict, list)):
                structured_content = getattr(result, "structured_content", None)
            if not isinstance(structured_content, (dict, list)):
                structured_content = {"cellId": cell_id, "updated": True}
            return ToolResult(
                content=result.content,
                structured_content=structured_content,
            )

        if self.name == "find_cells":
            import re

            query = arguments.get("query", "")
            use_regex = bool(arguments.get("regex", False))
            cell_type = arguments.get("cellType")
            include_outputs = bool(arguments.get("includeOutputs", False))
            cells_result = await self._call_colab(
                "get_cells", {"includeOutputs": include_outputs}
            )
            cells = json.loads(self._result_text(cells_result))["cells"]
            matches = []
            pattern = re.compile(query) if query and use_regex else None
            for index, cell in enumerate(cells):
                if cell_type and cell.get("cell_type") != cell_type:
                    continue
                haystack = self._cell_source(cell)
                if include_outputs:
                    haystack += "\n" + json.dumps(
                        cell.get("outputs", []), ensure_ascii=False
                    )
                matched = True
                if query:
                    matched = (
                        bool(pattern.search(haystack)) if pattern else query in haystack
                    )
                if matched:
                    matches.append({"index": index, "cell": cell})
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps({"matches": matches}, ensure_ascii=False),
                    )
                ],
                structured_content={"matches": matches},
            )

        if self.name == "wait_for_cells":
            timeout_seconds = float(arguments.get("timeoutSeconds", 60))
            interval = float(arguments.get("pollIntervalSeconds", 0.5))
            target_states = set(arguments.get("targetStates", ["success", "error"]))
            cell_ids = set(arguments["cellIds"])
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            statuses = []
            timed_out = False
            while True:
                cells_result = await self._call_colab(
                    "get_cells", {"includeOutputs": True}
                )
                cells = json.loads(self._result_text(cells_result))["cells"]
                statuses = [
                    self._cell_status(cell, index)
                    for index, cell in enumerate(cells)
                    if cell.get("id") in cell_ids
                ]
                if statuses and all(
                    status["state"] in target_states for status in statuses
                ):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    timed_out = True
                    break
                await asyncio.sleep(interval)
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"timedOut": timed_out, "cells": statuses},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                ],
                structured_content={"timedOut": timed_out, "cells": statuses},
            )

        if self.name in {
            "get_cell",
            "get_cell_status",
            "read_cell_outputs",
            "watch_cell_outputs",
        }:
            include_outputs = self.name != "get_cell" or bool(
                arguments.get("includeOutputs", False)
            )
            if self.name in {
                "get_cell_status",
                "read_cell_outputs",
                "watch_cell_outputs",
            }:
                include_outputs = True
            if self.name == "watch_cell_outputs":
                deadline = asyncio.get_running_loop().time() + float(
                    arguments.get("timeoutSeconds", 10)
                )
                interval = float(arguments.get("pollIntervalSeconds", 0.5))
                latest_cells = []
                while True:
                    cells_result = await self._call_colab(
                        "get_cells", {"includeOutputs": True}
                    )
                    latest_cells = json.loads(self._result_text(cells_result))["cells"]
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(interval)
                cells = latest_cells
            else:
                cells_result = await self._call_colab(
                    "get_cells", {"includeOutputs": include_outputs}
                )
                cells = json.loads(self._result_text(cells_result))["cells"]
            by_id = {cell.get("id"): (index, cell) for index, cell in enumerate(cells)}
            if self.name == "get_cell":
                cell_id = arguments["cellId"]
                if cell_id not in by_id:
                    raise ValueError(f"Cell not found: {cell_id}")
                _, cell = by_id[cell_id]
                return ToolResult(
                    content=[
                        TextContent(
                            type="text", text=json.dumps(cell, ensure_ascii=False)
                        )
                    ],
                    structured_content=cell,
                )
            cell_ids = arguments.get("cellIds") or list(by_id)
            missing_ids = [cell_id for cell_id in cell_ids if cell_id not in by_id]
            if missing_ids:
                raise ValueError(f"Cell(s) not found: {missing_ids}")
            if self.name == "get_cell_status":
                statuses = [
                    self._cell_status(by_id[cell_id][1], by_id[cell_id][0])
                    for cell_id in cell_ids
                    if cell_id in by_id
                ]
                if not bool(arguments.get("includeOutputs", False)):
                    for status in statuses:
                        status.pop("outputs", None)
                return ToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {"cells": statuses}, ensure_ascii=False, indent=2
                            ),
                        )
                    ],
                    structured_content={"cells": statuses},
                )
            max_bytes = int(arguments.get("maxBytes", 20000))
            normalize = bool(arguments.get("normalize", False))
            include_raw = bool(
                arguments.get("includeRaw", arguments.get("include_raw", False))
            )
            lines_arg = arguments.get("lines", 20)
            lines = int(lines_arg) if lines_arg is not None else 20
            raw_outputs = {
                cell_id: by_id[cell_id][1].get("outputs", [])
                for cell_id in cell_ids
                if cell_id in by_id
            }

            tailed_outputs = {}
            tailed_chunks = {}
            tail_texts = {}
            total_lines_map = {}
            returned_lines_map = {}

            for cell_id, cell_outputs in raw_outputs.items():
                norm_chunks = self._normalize_outputs(cell_outputs)
                chunks_tailed, tail_text, total_lines = self._tail_chunks_and_text(
                    norm_chunks, lines
                )
                if normalize:
                    tailed_chunks[cell_id] = chunks_tailed
                tail_texts[cell_id] = tail_text
                total_lines_map[cell_id] = total_lines
                returned_lines_map[cell_id] = (
                    len(tail_text.splitlines()) if tail_text else 0
                )
                if include_raw:
                    tailed_outputs[cell_id] = self._tail_raw_outputs(
                        cell_outputs, lines
                    )

            payload = {
                "tailText": tail_texts,
                "totalLines": total_lines_map,
                "returnedLines": returned_lines_map,
            }
            if include_raw:
                payload["outputs"] = tailed_outputs
            if normalize:
                payload["chunks"] = tailed_chunks
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            truncated = len(text.encode("utf-8")) > max_bytes
            if truncated:
                text = text.encode("utf-8")[:max_bytes].decode(
                    "utf-8", errors="replace"
                )
            structured_content = {
                "tailText": tail_texts,
                "totalLines": total_lines_map,
                "returnedLines": returned_lines_map,
                "truncated": truncated,
            }
            if include_raw:
                structured_content["outputs"] = tailed_outputs
            if normalize:
                structured_content["chunks"] = tailed_chunks

            return ToolResult(
                content=[TextContent(type="text", text=text)],
                structured_content=structured_content,
            )

        if self.name == "check_runtime":
            code = (
                "import json, os, platform, sys\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "packages = {}\n"
                "for name in ['numpy', 'torch', 'tensorflow', 'transformers']:\n"
                "    try:\n"
                "        module = __import__(name)\n"
                "        packages[name] = getattr(module, '__version__', 'unknown')\n"
                "    except Exception as exc:\n"
                "        packages[name] = None\n"
                "info = {\n"
                "    'python': sys.version,\n"
                "    'platform': platform.platform(),\n"
                "    'cpu_count': os.cpu_count(),\n"
                "    'cwd': os.getcwd(),\n"
                "    'packages': packages,\n"
                "    'executionBackend': 'colab-terminal',\n"
                "}\n"
                "emit(info)\n"
            )
            payload = await self._run_terminal_python(code, timeout_seconds=30)
            try:
                value = await asyncio.to_thread(
                    _evaluate_colab_page,
                    _runtime_accelerator_state_expression(),
                )
                payload["acceleratorUiState"] = json.loads(
                    value.get("result", {}).get("value", "{}")
                )
            except Exception as exc:
                payload.setdefault("warnings", []).append(
                    f"Could not inspect accelerator UI state: {exc}"
                )
            return self._terminal_tool_result(payload)

        if self.name == "check_gpu":
            code = (
                "import json, os, shutil, subprocess\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "info = {'executionBackend': 'colab-terminal', 'nvidiaSmiAvailable': bool(shutil.which('nvidia-smi')), 'warnings': [], 'recommendedNextActions': []}\n"
                "if shutil.which('nvidia-smi'):\n"
                "    proc = subprocess.run(['nvidia-smi'], text=True, capture_output=True)\n"
                "    query = subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,utilization.memory,temperature.gpu,power.draw', '--format=csv,noheader,nounits'], text=True, capture_output=True)\n"
                "    info['nvidiaSmi'] = {'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}\n"
                "    info['query'] = {'returncode': query.returncode, 'stdout': query.stdout, 'stderr': query.stderr}\n"
                "    info['gpus'] = []\n"
                "    for line in query.stdout.splitlines():\n"
                "        parts = [part.strip() for part in line.split(',')]\n"
                "        if len(parts) >= 9:\n"
                "            info['gpus'].append({'gpuIndex': parts[0], 'name': parts[1], 'memoryTotalMiB': parts[2], 'memoryUsedMiB': parts[3], 'memoryFreeMiB': parts[4], 'gpuUtilizationPercent': parts[5], 'memoryUtilizationPercent': parts[6], 'temperatureC': parts[7], 'powerDrawW': parts[8]})\n"
                "    if proc.returncode != 0 or query.returncode != 0:\n"
                "        info['warnings'].append('nvidia-smi failed in the connected runtime.')\n"
                "        info['recommendedNextActions'].extend(['connect_runtime(waitSeconds=180)', 'check_gpu()'])\n"
                "    elif not info['gpus']:\n"
                "        info['warnings'].append('nvidia-smi is available but no GPU rows were returned.')\n"
                "        info['recommendedNextActions'].extend(['set_runtime_accelerator(accelerator=\"T4 GPU\", apply=true)', 'open_colab_browser_connection()', 'connect_runtime(waitSeconds=180)', 'check_gpu()'])\n"
                "else:\n"
                "    info['nvidiaSmi'] = {'available': False}\n"
                "    info['gpus'] = []\n"
                "    info['warnings'].append('No GPU is active in the connected runtime because nvidia-smi is not available.')\n"
                "    info['recommendedNextActions'].extend(['set_runtime_accelerator(accelerator=\"T4 GPU\", apply=true)', 'open_colab_browser_connection()', 'connect_runtime(waitSeconds=180)', 'check_gpu()'])\n"
                "emit(info)\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name == "connect_runtime":
            wait_seconds = int(arguments.get("waitSeconds", 180))
            info = self._proxy_client.wss.connection_info()
            browser_connected = self._proxy_client.is_connected()
            if not browser_connected:
                active_url = get_active_notebook_url()
                target_url = (
                    f"{resolve_notebook_url(active_url)}#mcpProxyToken={info['token']}&mcpProxyPort={info['port']}"
                    if active_url
                    else info["scratchUrl"]
                )
                browser_connected = _navigate_controlled_edge(
                    target_url, token=info["token"], mcp_port=info["port"]
                )
            value = await asyncio.to_thread(
                _evaluate_colab_page,
                _runtime_connection_expression(wait_seconds),
                await_promise=True,
            )
            payload = json.loads(value.get("result", {}).get("value", "{}"))
            payload["browserConnected"] = browser_connected
            payload["executionBackend"] = "colab-browser-cdp"
            warnings = payload.get("warnings", [])
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                ],
                structured_content=self._structured(
                    payload,
                    ok=bool(payload.get("ok")),
                    status="ok" if payload.get("ok") and not warnings else "warning",
                    warnings=warnings,
                ),
            )

        if self.name == "set_runtime_accelerator":
            accelerator = arguments.get("accelerator") or "T4 GPU"
            apply = bool(arguments.get("apply", True))
            info = self._proxy_client.wss.connection_info()
            connected = self._proxy_client.is_connected()
            if not connected:
                active_url = get_active_notebook_url()
                target_url = (
                    f"{resolve_notebook_url(active_url)}#mcpProxyToken={info['token']}&mcpProxyPort={info['port']}"
                    if active_url
                    else info["scratchUrl"]
                )
                connected = _navigate_controlled_edge(
                    target_url, token=info["token"], mcp_port=info["port"]
                )
            value = await asyncio.to_thread(
                _evaluate_colab_page,
                _runtime_accelerator_expression(accelerator, apply),
                await_promise=True,
            )
            payload = json.loads(value.get("result", {}).get("value", "{}"))
            payload["browserConnected"] = connected
            payload["executionBackend"] = "colab-browser-cdp"
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                ],
                structured_content=self._structured(
                    payload,
                    ok=bool(payload.get("ok")),
                    status="ok" if payload.get("ok") else "warning",
                    warnings=payload.get("warnings", []),
                ),
            )

        if self.name == "resource_usage_snapshot":
            save_path = arguments.get("savePath", "/content/colab_mcp_usage.jsonl")
            code = (
                "import json, os, shutil, subprocess, time\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "snapshot = {'timestamp': time.time(), 'executionBackend': 'colab-terminal'}\n"
                "try:\n"
                "    import psutil\n"
                "    snapshot['cpu_percent'] = psutil.cpu_percent(interval=0.2)\n"
                "    snapshot['memory'] = psutil.virtual_memory()._asdict()\n"
                "    snapshot['disk'] = psutil.disk_usage('/content')._asdict()\n"
                "except Exception as exc:\n"
                "    snapshot['psutil_error'] = repr(exc)\n"
                "if shutil.which('nvidia-smi'):\n"
                "    proc = subprocess.run([\n"
                "        'nvidia-smi',\n"
                "        '--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,utilization.memory,temperature.gpu,power.draw',\n"
                "        '--format=csv,noheader,nounits'\n"
                "    ], text=True, capture_output=True)\n"
                "    snapshot['nvidia_smi_query'] = {'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}\n"
                "    snapshot['gpus'] = []\n"
                "    for line in proc.stdout.splitlines():\n"
                "        parts = [part.strip() for part in line.split(',')]\n"
                "        if len(parts) >= 9:\n"
                "            snapshot['gpus'].append({'gpuIndex': parts[0], 'name': parts[1], 'memoryTotalMiB': parts[2], 'memoryUsedMiB': parts[3], 'memoryFreeMiB': parts[4], 'gpuUtilizationPercent': parts[5], 'memoryUtilizationPercent': parts[6], 'temperatureC': parts[7], 'powerDrawW': parts[8]})\n"
                "else:\n"
                "    snapshot['nvidia_smi_query'] = {'available': False}\n"
                "    snapshot['gpus'] = []\n"
                f"save_path = {save_path!r}\n"
                "if save_path:\n"
                "    os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)\n"
                "    with open(save_path, 'a', encoding='utf-8') as f:\n"
                "        f.write(json.dumps(snapshot, sort_keys=True) + '\\n')\n"
                "    snapshot['saved_to'] = save_path\n"
                "emit(snapshot)\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name == "run_shell_command":
            command = arguments["command"]
            timeout_seconds = int(arguments.get("timeoutSeconds", 600))
            cwd = arguments.get("cwd")
            code = (
                "import base64, json, os, subprocess\n"
                f"command = {command!r}\n"
                f"timeout_seconds = {timeout_seconds!r}\n"
                f"cwd = {cwd!r}\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "try:\n"
                "    proc = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=timeout_seconds, cwd=cwd or None)\n"
                "    result = {'command': command, 'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr, 'timed_out': False, 'executionBackend': 'colab-terminal'}\n"
                "    if proc.returncode != 0:\n"
                "        result['error'] = f'Command exited with return code {proc.returncode}. Fix the command or environment, then rerun.'\n"
                "        result['recommendedNextActions'] = ['Inspect stdout/stderr from this result', 'Fix the command, cwd, dependencies, or runtime state', 'Rerun run_shell_command for short commands or start_background_command for long jobs']\n"
                "except subprocess.TimeoutExpired as exc:\n"
                "    result = {'command': command, 'returncode': None, 'stdout': exc.stdout or '', 'stderr': exc.stderr or '', 'timed_out': True, 'timeout_seconds': timeout_seconds, 'executionBackend': 'colab-terminal'}\n"
                "    result['error'] = f'Command timed out after {timeout_seconds} seconds. Do not keep retrying the same synchronous call for long work.'\n"
                "    result['recommendedNextActions'] = ['Use start_background_command for long jobs such as training', 'Increase timeoutSeconds only for bounded short commands', 'Inspect partial stdout/stderr from this result']\n"
                "emit(result)\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(
                    code, timeout_seconds=timeout_seconds + 5
                )
            )

        if self.name == "start_background_command":
            command = arguments["command"]
            name = arguments.get("name") or "job"
            log_path = arguments.get("logPath")
            cwd = arguments.get("cwd")
            code = (
                "import base64, json, os, re, subprocess, sys, textwrap, time\n"
                f"command = {command!r}\n"
                f"name = {name!r}\n"
                f"log_path = {log_path!r}\n"
                f"cwd = {cwd!r}\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "safe_name = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_') or 'job'\n"
                "registry_path = '/content/.colab_mcp_processes.json'\n"
                "log_dir = '/content/colab_mcp_logs'\n"
                "os.makedirs(log_dir, exist_ok=True)\n"
                "if not log_path:\n"
                "    log_path = os.path.join(log_dir, safe_name + '.log')\n"
                "os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)\n"
                "status_path = os.path.join(log_dir, safe_name + '.status.json')\n"
                "supervisor_path = os.path.join(log_dir, safe_name + '.supervisor.py')\n"
                'supervisor = f"""\n'
                "import json, os, subprocess, time\n"
                "command = {command!r}\n"
                "name = {name!r}\n"
                "log_path = {log_path!r}\n"
                "status_path = {status_path!r}\n"
                "cwd = {cwd!r}\n"
                "def write_status(payload):\n"
                "    tmp = status_path + '.tmp'\n"
                "    with open(tmp, 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, indent=2, sort_keys=True)\n"
                "    os.replace(tmp, status_path)\n"
                "started_at = time.time()\n"
                "status = {{'name': name, 'command': command, 'logPath': log_path, 'cwd': cwd, 'startedAt': started_at, 'running': True, 'returncode': None}}\n"
                "try:\n"
                "    with open(log_path, 'ab', buffering=0) as log:\n"
                "        proc = subprocess.Popen(command, shell=True, stdout=log, stderr=subprocess.STDOUT, cwd=cwd or None)\n"
                "        status['commandPid'] = proc.pid\n"
                "        write_status(status)\n"
                "        returncode = proc.wait()\n"
                "        status.update({{'running': False, 'returncode': returncode, 'finishedAt': time.time(), 'durationSeconds': time.time() - started_at}})\n"
                "        write_status(status)\n"
                "except BaseException as exc:\n"
                "    status.update({{'running': False, 'returncode': None, 'finishedAt': time.time(), 'durationSeconds': time.time() - started_at, 'error': repr(exc)}})\n"
                "    write_status(status)\n"
                '"""\n'
                "with open(supervisor_path, 'w', encoding='utf-8') as f:\n"
                "    f.write(textwrap.dedent(supervisor).lstrip())\n"
                "proc = subprocess.Popen([sys.executable, '-u', supervisor_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
                "try:\n"
                "    with open(registry_path, 'r', encoding='utf-8') as f:\n"
                "        registry = json.load(f)\n"
                "except Exception:\n"
                "    registry = {}\n"
                "registry[name] = {'pid': proc.pid, 'command': command, 'logPath': log_path, 'statusPath': status_path, 'supervisorPath': supervisor_path, 'cwd': cwd, 'startedAt': time.time()}\n"
                "with open(registry_path, 'w', encoding='utf-8') as f:\n"
                "    json.dump(registry, f, indent=2, sort_keys=True)\n"
                "emit({'name': name, 'pid': proc.pid, 'logPath': log_path, 'statusPath': status_path, 'registryPath': registry_path, 'executionBackend': 'colab-terminal'})\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name == "check_background_command":
            name = arguments["name"]
            code = (
                "import base64, json, os, signal, time\n"
                f"name = {name!r}\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "registry_path = '/content/.colab_mcp_processes.json'\n"
                "with open(registry_path, 'r', encoding='utf-8') as f:\n"
                "    registry = json.load(f)\n"
                "entry = registry[name]\n"
                "pid = int(entry['pid'])\n"
                "running = True\n"
                "try:\n"
                "    os.kill(pid, 0)\n"
                "except OSError:\n"
                "    running = False\n"
                "entry = dict(entry)\n"
                "status_path = entry.get('statusPath')\n"
                "if status_path and os.path.exists(status_path):\n"
                "    with open(status_path, 'r', encoding='utf-8') as f:\n"
                "        entry.update(json.load(f))\n"
                "entry.update({'name': name, 'running': running and entry.get('running', running), 'checkedAt': time.time()})\n"
                "entry['executionBackend'] = 'colab-terminal'\n"
                "emit(entry)\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name == "tail_file":
            path = arguments["path"]
            lines = int(arguments.get("lines", 80))
            max_bytes = int(arguments.get("maxBytes", 20000))
            code = (
                "import base64, json, os\n"
                f"path = {path!r}\n"
                f"lines = {lines!r}\n"
                f"max_bytes = {max_bytes!r}\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "size = os.path.getsize(path)\n"
                "with open(path, 'rb') as f:\n"
                "    f.seek(max(0, size - max_bytes))\n"
                "    data = f.read().decode('utf-8', errors='replace')\n"
                "tail = '\\n'.join(data.splitlines()[-lines:])\n"
                "emit({'path': path, 'size': size, 'tail': tail, 'executionBackend': 'colab-terminal'})\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name == "stop_background_command":
            name = arguments["name"]
            sig = int(arguments.get("signal", 15))
            code = (
                "import base64, json, os, signal, time\n"
                f"name = {name!r}\n"
                f"sig = {sig!r}\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "registry_path = '/content/.colab_mcp_processes.json'\n"
                "with open(registry_path, 'r', encoding='utf-8') as f:\n"
                "    registry = json.load(f)\n"
                "entry = registry[name]\n"
                "pid = int(entry['pid'])\n"
                "sent = False\n"
                "try:\n"
                "    os.killpg(pid, sig)\n"
                "    sent = True\n"
                "except Exception:\n"
                "    try:\n"
                "        os.kill(pid, sig)\n"
                "        sent = True\n"
                "    except OSError:\n"
                "        sent = False\n"
                "entry = dict(entry)\n"
                "entry.update({'name': name, 'signal': sig, 'sent': sent, 'stoppedAt': time.time()})\n"
                "entry['executionBackend'] = 'colab-terminal'\n"
                "emit(entry)\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name in {"list_background_commands", "watch_background_command"}:
            name = arguments.get("name")
            lines = int(arguments.get("lines", 80))
            code = (
                "import base64, json, os, time\n"
                "registry_path = '/content/.colab_mcp_processes.json'\n"
                f"name = {name!r}\n"
                f"lines = {lines!r}\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "try:\n"
                "    with open(registry_path, 'r', encoding='utf-8') as f:\n"
                "        registry = json.load(f)\n"
                "except Exception:\n"
                "    registry = {}\n"
                "def is_running(pid):\n"
                "    try:\n"
                "        os.kill(int(pid), 0)\n"
                "        return True\n"
                "    except Exception:\n"
                "        return False\n"
                "entries = []\n"
                "for key, entry in registry.items():\n"
                "    item = dict(entry)\n"
                "    item['name'] = key\n"
                "    item['running'] = is_running(item.get('pid'))\n"
                "    item['checkedAt'] = time.time()\n"
                "    status_path = item.get('statusPath')\n"
                "    if status_path and os.path.exists(status_path):\n"
                "        with open(status_path, 'r', encoding='utf-8') as f:\n"
                "            item.update(json.load(f))\n"
                "        item['running'] = item.get('running', False) and is_running(item.get('pid'))\n"
                "    if name and key != name:\n"
                "        continue\n"
                "    if name:\n"
                "        log_path = item.get('logPath')\n"
                "        if log_path and os.path.exists(log_path):\n"
                "            with open(log_path, 'rb') as f:\n"
                "                data = f.read()[-20000:].decode('utf-8', errors='replace')\n"
                "            item['tail'] = '\\n'.join(data.splitlines()[-lines:])\n"
                "    item['executionBackend'] = 'colab-terminal'\n"
                "    entries.append(item)\n"
                "emit({'commands': entries, 'executionBackend': 'colab-terminal'})\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name in {
            "upload_file",
            "upload_local_file",
            "download_file",
            "download_file_to_local",
            "upload_file_chunk",
            "complete_upload",
            "download_file_chunk",
            "stat_file",
            "list_files",
            "delete_file",
            "make_directory",
        }:
            if self.name == "upload_file":
                result = await asyncio.to_thread(
                    _runtime_upload_file,
                    arguments["path"],
                    arguments["contentBase64"],
                    overwrite=bool(arguments.get("overwrite", False)),
                    make_parents=bool(arguments.get("makeParents", True)),
                )
                return self._runtime_tool_result(result)
            if self.name == "upload_local_file":
                result = await asyncio.to_thread(
                    _runtime_upload_local_file,
                    arguments["localPath"],
                    arguments["path"],
                    overwrite=bool(arguments.get("overwrite", False)),
                    make_parents=bool(arguments.get("makeParents", True)),
                )
                return self._runtime_tool_result(result)
            if self.name == "download_file":
                result = await asyncio.to_thread(
                    _runtime_download_file,
                    arguments["path"],
                    offset=int(arguments.get("offset", 0)),
                    max_bytes=arguments.get("maxBytes"),
                )
                return self._runtime_tool_result(result)
            if self.name == "download_file_to_local":
                result = await asyncio.to_thread(
                    _runtime_download_file_to_local,
                    arguments["path"],
                    arguments["localPath"],
                    overwrite=bool(arguments.get("overwrite", False)),
                    make_parents=bool(arguments.get("makeParents", True)),
                )
                return self._runtime_tool_result(result)
            if self.name == "upload_file_chunk":
                result = await asyncio.to_thread(
                    _runtime_upload_file_chunk,
                    arguments["uploadId"],
                    arguments["path"],
                    arguments["chunkBase64"],
                    chunk_index=int(arguments["chunkIndex"]),
                    overwrite=bool(arguments.get("overwrite", False)),
                )
                return self._runtime_tool_result(result)
            if self.name == "complete_upload":
                result = await asyncio.to_thread(
                    _runtime_complete_upload,
                    arguments["uploadId"],
                    arguments["path"],
                    overwrite=bool(arguments.get("overwrite", False)),
                )
                return self._runtime_tool_result(result)
            if self.name == "download_file_chunk":
                result = await asyncio.to_thread(
                    _runtime_download_file_chunk,
                    arguments["path"],
                    offset=int(arguments.get("offset", 0)),
                    max_bytes=int(arguments.get("maxBytes", 1048576)),
                )
                return self._runtime_tool_result(result)
            if self.name == "stat_file":
                result = await asyncio.to_thread(_runtime_stat_file, arguments["path"])
                return self._runtime_tool_result(result)
            if self.name == "list_files":
                result = await asyncio.to_thread(
                    _runtime_list_files,
                    arguments.get("path", "."),
                    recursive=bool(arguments.get("recursive", False)),
                    max_entries=int(arguments.get("maxEntries", 1000)),
                )
                return self._runtime_tool_result(result)
            if self.name == "delete_file":
                result = await asyncio.to_thread(
                    _runtime_delete_file,
                    arguments["path"],
                    recursive=bool(arguments.get("recursive", False)),
                    force=bool(arguments.get("force", False)),
                )
                return self._runtime_tool_result(result)
            if self.name == "make_directory":
                result = await asyncio.to_thread(
                    _runtime_make_directory,
                    arguments["path"],
                    parents=bool(arguments.get("parents", True)),
                    exist_ok=bool(arguments.get("existOk", True)),
                )
                return self._runtime_tool_result(result)

        if self.name == "sample_gpu_usage":
            interval = float(arguments.get("intervalSeconds", 1.0))
            count = int(arguments.get("count", 1))
            save_path = arguments.get("savePath", "/content/colab_mcp_gpu.jsonl")
            code = (
                "import datetime, json, os, shutil, subprocess, time\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                f"interval = {interval!r}\n"
                f"count = {count!r}\n"
                f"save_path = {save_path!r}\n"
                "samples = []\n"
                "for sample_index in range(count):\n"
                "    now = time.time()\n"
                "    sample = {'timestamp': now, 'isoTimestamp': datetime.datetime.fromtimestamp(now, datetime.UTC).isoformat(), 'sampleIndex': sample_index, 'executionBackend': 'colab-terminal'}\n"
                "    if shutil.which('nvidia-smi'):\n"
                "        proc = subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,utilization.memory,temperature.gpu,power.draw', '--format=csv,noheader,nounits'], text=True, capture_output=True)\n"
                "        sample['nvidiaSmiReturnCode'] = proc.returncode\n"
                "        sample['gpus'] = []\n"
                "        for line in proc.stdout.splitlines():\n"
                "            parts = [part.strip() for part in line.split(',')]\n"
                "            if len(parts) >= 9:\n"
                "                sample['gpus'].append({'gpuIndex': parts[0], 'name': parts[1], 'memoryTotalMiB': parts[2], 'memoryUsedMiB': parts[3], 'memoryFreeMiB': parts[4], 'gpuUtilizationPercent': parts[5], 'memoryUtilizationPercent': parts[6], 'temperatureC': parts[7], 'powerDrawW': parts[8]})\n"
                "        if proc.stderr:\n"
                "            sample['nvidiaSmiStderr'] = proc.stderr\n"
                "    else:\n"
                "        sample['gpus'] = []\n"
                "        sample['nvidiaSmiAvailable'] = False\n"
                "    samples.append(sample)\n"
                "    if sample_index + 1 < count and interval > 0:\n"
                "        time.sleep(interval)\n"
                "if save_path:\n"
                "    os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)\n"
                "    with open(save_path, 'a', encoding='utf-8') as f:\n"
                "        for sample in samples:\n"
                "            f.write(json.dumps(sample, sort_keys=True) + '\\n')\n"
                "emit({'samples': samples, 'savePath': save_path, 'executionBackend': 'colab-terminal'})\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(
                    code, timeout_seconds=int(interval * max(count - 1, 0)) + 30
                )
            )

        if self.name in {"start_gpu_monitor", "stop_gpu_monitor", "read_gpu_monitor"}:
            name = arguments.get("name") or "gpu"
            if self.name == "start_gpu_monitor":
                interval = float(arguments.get("intervalSeconds", 1.0))
                save_path = arguments.get("savePath", "/content/colab_mcp_gpu.jsonl")
                command = (
                    "python -u - <<'PY'\n"
                    "import datetime, json, os, shutil, subprocess, time\n"
                    f"interval = {interval!r}\n"
                    f"save_path = {save_path!r}\n"
                    "os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)\n"
                    "def sample_once(i):\n"
                    "    now = time.time()\n"
                    "    sample = {'timestamp': now, 'isoTimestamp': datetime.datetime.fromtimestamp(now, datetime.UTC).isoformat(), 'sampleIndex': i}\n"
                    "    if shutil.which('nvidia-smi'):\n"
                    "        proc = subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,utilization.memory,temperature.gpu,power.draw', '--format=csv,noheader,nounits'], text=True, capture_output=True)\n"
                    "        sample['gpus'] = []\n"
                    "        for line in proc.stdout.splitlines():\n"
                    "            parts = [part.strip() for part in line.split(',')]\n"
                    "            if len(parts) >= 9:\n"
                    "                sample['gpus'].append({'gpuIndex': parts[0], 'name': parts[1], 'memoryTotalMiB': parts[2], 'memoryUsedMiB': parts[3], 'memoryFreeMiB': parts[4], 'gpuUtilizationPercent': parts[5], 'memoryUtilizationPercent': parts[6], 'temperatureC': parts[7], 'powerDrawW': parts[8]})\n"
                    "    else:\n"
                    "        sample['gpus'] = []\n"
                    "        sample['nvidiaSmiAvailable'] = False\n"
                    "    return sample\n"
                    "i = 0\n"
                    "with open(save_path, 'a', encoding='utf-8') as f:\n"
                    "    while True:\n"
                    "        f.write(json.dumps(sample_once(i), sort_keys=True) + '\\n')\n"
                    "        f.flush()\n"
                    "        i += 1\n"
                    "        time.sleep(interval)\n"
                    "PY"
                )
                log_path = f"/content/colab_mcp_logs/{name}.gpu.log"
                code = (
                    "import json, os, re, subprocess, time\n"
                    f"command = {command!r}\n"
                    f"name = {name!r}\n"
                    f"log_path = {log_path!r}\n"
                    f"save_path = {save_path!r}\n"
                    "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                    "def emit(payload):\n"
                    "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                    "        json.dump(payload, f, ensure_ascii=False)\n"
                    "    print(f'{marker}RESULT{marker}', flush=True)\n"
                    "safe_name = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_') or 'gpu'\n"
                    "registry_path = '/content/.colab_mcp_processes.json'\n"
                    "os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)\n"
                    "log = open(log_path, 'ab', buffering=0)\n"
                    "proc = subprocess.Popen(command, shell=True, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)\n"
                    "try:\n"
                    "    with open(registry_path, 'r', encoding='utf-8') as f:\n"
                    "        registry = json.load(f)\n"
                    "except Exception:\n"
                    "    registry = {}\n"
                    "registry[name] = {'pid': proc.pid, 'command': command, 'logPath': log_path, 'cwd': None, 'startedAt': time.time(), 'kind': 'gpu_monitor'}\n"
                    "with open(registry_path, 'w', encoding='utf-8') as f:\n"
                    "    json.dump(registry, f, indent=2, sort_keys=True)\n"
                    "emit({'name': name, 'pid': proc.pid, 'logPath': log_path, 'registryPath': registry_path, 'savePath': save_path, 'executionBackend': 'colab-terminal'})\n"
                )
                return self._terminal_tool_result(
                    await self._run_terminal_python(code, timeout_seconds=30)
                )
            if self.name == "stop_gpu_monitor":
                sig = 15
                code = (
                    "import json, os, signal, time\n"
                    f"name = {name!r}\n"
                    f"sig = {sig!r}\n"
                    "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                    "def emit(payload):\n"
                    "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                    "        json.dump(payload, f, ensure_ascii=False)\n"
                    "    print(f'{marker}RESULT{marker}', flush=True)\n"
                    "registry_path = '/content/.colab_mcp_processes.json'\n"
                    "with open(registry_path, 'r', encoding='utf-8') as f:\n"
                    "    registry = json.load(f)\n"
                    "entry = registry[name]\n"
                    "pid = int(entry['pid'])\n"
                    "sent = False\n"
                    "try:\n"
                    "    os.killpg(pid, sig)\n"
                    "    sent = True\n"
                    "except Exception:\n"
                    "    try:\n"
                    "        os.kill(pid, sig)\n"
                    "        sent = True\n"
                    "    except OSError:\n"
                    "        sent = False\n"
                    "entry = dict(entry)\n"
                    "entry.update({'name': name, 'signal': sig, 'sent': sent, 'stoppedAt': time.time(), 'executionBackend': 'colab-terminal'})\n"
                    "emit(entry)\n"
                )
                return self._terminal_tool_result(
                    await self._run_terminal_python(code, timeout_seconds=30)
                )
            path = arguments.get("path") or "/content/colab_mcp_gpu.jsonl"
            lines = int(arguments.get("lines", 100))
            code = (
                "import json, os\n"
                f"path = {path!r}\n"
                f"lines = {lines!r}\n"
                "marker = os.environ['COLAB_MCP_RESULT_MARKER']\n"
                "def emit(payload):\n"
                "    with open(os.environ['COLAB_MCP_RESULT_PATH'], 'w', encoding='utf-8') as f:\n"
                "        json.dump(payload, f, ensure_ascii=False)\n"
                "    print(f'{marker}RESULT{marker}', flush=True)\n"
                "size = os.path.getsize(path)\n"
                "with open(path, 'rb') as f:\n"
                "    data = f.read()[-20000:].decode('utf-8', errors='replace')\n"
                "tail = '\\n'.join(data.splitlines()[-lines:])\n"
                "emit({'path': path, 'size': size, 'tail': tail, 'executionBackend': 'colab-terminal'})\n"
            )
            return self._terminal_tool_result(
                await self._run_terminal_python(code, timeout_seconds=30)
            )

        if self.name in {"import_notebook", "upload_notebook"}:
            path = Path(arguments["path"]).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"Notebook not found: {path}")
            if path.suffix.lower() != ".ipynb":
                raise ValueError(f"Expected a .ipynb file: {path}")
            notebook = json.loads(path.read_text(encoding="utf-8"))
            source_cells = notebook.get("cells", [])

            if arguments.get("replaceExisting", False):
                existing = await self._call_colab(
                    "get_cells", {"includeOutputs": False}
                )
                for cell in reversed(json.loads(self._result_text(existing))["cells"]):
                    await self._call_colab("delete_cell", {"cellId": cell["id"]})

            cell_index = arguments.get("cellIndex")
            if cell_index is None:
                cells = await self._call_colab("get_cells", {"includeOutputs": False})
                cell_index = len(json.loads(self._result_text(cells))["cells"])

            imported = []
            for source_cell in source_cells:
                cell_type = source_cell.get("cell_type")
                source = self._cell_source(source_cell)
                if cell_type == "code":
                    added = await self._call_colab(
                        "add_code_cell",
                        {
                            "cellIndex": cell_index,
                            "language": "python",
                            "code": source,
                        },
                    )
                elif cell_type == "markdown":
                    added = await self._call_colab(
                        "add_text_cell",
                        {"cellIndex": cell_index, "content": source},
                    )
                else:
                    continue
                cell_id = json.loads(self._result_text(added))["newCellId"]
                imported.append({"cellId": cell_id, "cellType": cell_type})
                cell_index += 1

            payload = {"path": str(path), "imported": imported}
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                ],
                structured_content=payload,
            )

        if self.name in {"export_notebook", "download_notebook"}:
            path = Path(arguments["path"]).expanduser()
            if path.suffix.lower() != ".ipynb":
                raise ValueError(f"Expected a .ipynb destination path: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            include_outputs = bool(arguments.get("includeOutputs", True))
            cells_result = await self._call_colab(
                "get_cells", {"includeOutputs": include_outputs}
            )
            cells = json.loads(self._result_text(cells_result))["cells"]
            notebook = self._notebook_document(cells, name=path.name)
            path.write_text(
                json.dumps(notebook, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            payload = {
                "path": str(path),
                "cellCount": len(cells),
                "includeOutputs": include_outputs,
            }
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                ],
                structured_content=payload,
            )

        reason = arguments.get("reason") or self.description or self.name
        if self.name == "restart_runtime":
            code = (
                f"print({reason!r})\n"
                "import os, signal\n"
                "try:\n"
                "    from google.colab import runtime\n"
                "    restart = getattr(runtime, 'restart_runtime', None)\n"
                "    if restart is not None:\n"
                "        restart()\n"
                "    else:\n"
                "        os.kill(os.getpid(), signal.SIGKILL)\n"
                "except Exception:\n"
                "    os.kill(os.getpid(), signal.SIGKILL)\n"
            )
        elif self.name == "shutdown_runtime":
            code = (
                f"print({reason!r})\n"
                "from google.colab import runtime\n"
                "runtime.unassign()\n"
            )
        else:
            raise RuntimeError(f"Unknown Colab management tool: {self.name}")

        cell_id, result = await self._append_and_run_code(code, delete_after=True)
        return ToolResult(
            content=result.content,
            structured_content={"cellId": cell_id, "requested": self.name},
        )


def make_static_colab_proxy_tools(proxy_client: ColabProxyClient) -> list[Tool]:
    return [
        StaticColabProxyTool(
            proxy_client=proxy_client,
            upstream_name=name,
            description=spec["description"],
            parameters=deepcopy(spec["parameters"]),
        )
        for name, spec in COLAB_STATIC_TOOL_SCHEMAS.items()
    ]


def make_colab_management_tools(proxy_client: ColabProxyClient) -> list[Tool]:
    return [
        ColabRuntimeManagementTool(
            proxy_client=proxy_client,
            name=name,
            description=spec["description"],
            parameters=deepcopy(spec["parameters"]),
        )
        for name, spec in COLAB_MANAGEMENT_TOOL_SCHEMAS.items()
    ]


class ColabProxyMiddleware(Middleware):
    def __init__(self, proxy_client: ColabProxyClient):
        self.proxy_client = proxy_client
        self.last_message_connected = self.proxy_client.is_connected()

    async def on_message(self, context: MiddlewareContext, call_next):
        """
        Check for a change to Colab session connectivity on any communication with this MCP server and
        notify the client when the connectivity status has changed.
        """
        context.fastmcp_context.set_state(
            FE_CONNECTED_KEY, self.proxy_client.is_connected()
        )
        context.fastmcp_context.set_state(PROXY_TOKEN_KEY, self.proxy_client.wss.token)
        context.fastmcp_context.set_state(PROXY_PORT_KEY, self.proxy_client.wss.port)

        result = await call_next(context)

        connected = self.proxy_client.is_connected()
        connection_state_changed = connected != self.last_message_connected
        self.last_message_connected = connected
        if connection_state_changed:
            await context.fastmcp_context.send_tool_list_changed()

        return result

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if context.message.name != INJECTED_TOOL_NAME:
            return result
        if self.proxy_client.is_connected():
            return result
        # if the tool call was for open_colab_browser_connection and there is no existing connection, try to await full connection
        await context.fastmcp_context.report_progress(
            progress=1, total=3, message="Colab UI session is not yet connected"
        )
        await context.fastmcp_context.report_progress(
            progress=2,
            total=3,
            message="Waiting for Colab WebSocket handshake to complete (waiting up to 10s)...",
        )
        await self.proxy_client.await_proxy_connection(timeout=10.0)
        if self.proxy_client.is_connected():
            await context.fastmcp_context.report_progress(
                progress=3, total=3, message="The Colab UI is successfully connected!"
            )
            return ToolResult(
                content=[TextContent(type="text", text="true")],
                structured_content={"result": True},
            )
        else:
            await context.fastmcp_context.report_progress(
                progress=3,
                total=3,
                message="Automated connection to Colab timed out.",
            )
            return ToolResult(
                content=[TextContent(type="text", text="false")],
                structured_content={"result": False},
            )


_ACTIVE_NOTEBOOK_URL: str | None = None


def get_active_notebook_url() -> str | None:
    global _ACTIVE_NOTEBOOK_URL
    return _ACTIVE_NOTEBOOK_URL


def set_active_notebook_url(url: str | None) -> None:
    global _ACTIVE_NOTEBOOK_URL
    if url and str(url).strip():
        _ACTIVE_NOTEBOOK_URL = str(url).strip()


def resolve_notebook_url(notebook_url: str | None = None) -> str:
    if not notebook_url or not str(notebook_url).strip():
        return f"{COLAB}{SCRATCH_PATH}"
    url = str(notebook_url).strip().split("#")[0]
    if url.startswith("/"):
        url = f"{COLAB}{url}"
    return url


def _is_target_tab_already_active(target_clean: str) -> bool:
    port = os.environ.get(EDGE_CDP_PORT_ENV, DEFAULT_EDGE_CDP_PORT)
    url_contains = os.environ.get(
        EDGE_CDP_URL_CONTAINS_ENV, "colab.research.google.com"
    )
    try:
        if not _cdp_alive(port):
            return False
        pages = [
            tab
            for tab in _cdp_json(f"http://127.0.0.1:{port}/json/list")
            if tab.get("type") == "page"
        ]
        candidates = [tab for tab in pages if url_contains in tab.get("url", "")]
        for tab in candidates:
            tab_base = tab.get("url", "").split("#")[0].split("?")[0]
            if target_clean in tab_base or tab_base in target_clean:
                return True
        return False
    except Exception:
        return False


async def check_session_proxy_tool_fn(
    ctx: Context = CurrentContext(),
    notebook_url: str | None = None,
) -> bool:
    fe_connected = ctx.get_state(FE_CONNECTED_KEY)
    token = ctx.get_state(PROXY_TOKEN_KEY)
    port = ctx.get_state(PROXY_PORT_KEY)
    active_url = get_active_notebook_url()
    target_url_str = notebook_url or active_url
    base_url = resolve_notebook_url(target_url_str)

    if fe_connected:
        target_clean = base_url.split("#")[0].split("?")[0]
        active_clean = (
            resolve_notebook_url(active_url).split("#")[0].split("?")[0]
            if active_url
            else None
        )
        if not notebook_url or (active_clean and target_clean == active_clean):
            if notebook_url:
                set_active_notebook_url(notebook_url)
            logging.info(
                "open_colab_browser_connection is already connected to target notebook; returning True (idempotent)."
            )
            return True
        if _is_target_tab_already_active(target_clean):
            set_active_notebook_url(notebook_url)
            logging.info(
                "open_colab_browser_connection: active browser tab already matches target notebook; returning True (idempotent)."
            )
            return True

    if notebook_url:
        set_active_notebook_url(notebook_url)
    url = f"{base_url}#mcpProxyToken={token}&mcpProxyPort={port}"
    return _navigate_controlled_edge(url, token=token, mcp_port=port)


def _cdp_json(url: str, *, method: str = "GET"):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _cdp_request(url: str, *, method: str = "GET"):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=2) as response:
        response.read()


def _cdp_alive(port: str) -> bool:
    try:
        _cdp_json(f"http://127.0.0.1:{port}/json/version")
        return True
    except Exception:
        return False


def _edge_executable_path() -> str:
    configured = os.environ.get(EDGE_PATH_ENV)
    if configured:
        path = Path(configured)
        if path.exists():
            return str(path)
        raise RuntimeError(f"Configured Edge executable was not found: {configured}")

    candidates = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            candidates.append(
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
    candidates.extend(
        [
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    for name in ("msedge", "microsoft-edge", "microsoft-edge-stable"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("Microsoft Edge executable was not found.")


def _ensure_controlled_edge(url: str, *, port: str) -> None:
    if _cdp_alive(port):
        return

    profile = Path(os.environ.get(EDGE_PROFILE_ENV, str(DEFAULT_EDGE_PROFILE)))
    profile.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    for flag_name in (
        "CREATE_NO_WINDOW",
        "CREATE_NEW_PROCESS_GROUP",
        "DETACHED_PROCESS",
        "CREATE_BREAKAWAY_FROM_JOB",
    ):
        creationflags |= getattr(subprocess, flag_name, 0)
    subprocess.Popen(
        [
            _edge_executable_path(),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--new-window",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _cdp_alive(port):
            return
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for Edge CDP on port {port}.")


def _colab_page_websocket_url() -> str:
    port = os.environ.get(EDGE_CDP_PORT_ENV, DEFAULT_EDGE_CDP_PORT)
    url_contains = os.environ.get(
        EDGE_CDP_URL_CONTAINS_ENV, "colab.research.google.com"
    )
    pages = [
        tab
        for tab in _cdp_json(f"http://127.0.0.1:{port}/json/list")
        if tab.get("type") == "page"
    ]
    candidates = [tab for tab in pages if url_contains in tab.get("url", "")]
    if not candidates:
        raise RuntimeError(
            "No controlled Colab tab found. Call open_colab_browser_connection first."
        )

    active_url = get_active_notebook_url()
    target = None
    if active_url:
        clean_active = active_url.split("#")[0].split("?")[0]
        if clean_active and not clean_active.endswith("empty.ipynb"):
            target = next(
                (tab for tab in candidates if clean_active in tab.get("url", "")), None
            )
    if target is None:
        target = next(
            (tab for tab in candidates if "empty.ipynb" not in tab.get("url", "")),
            None,
        )
    if target is None:
        target = candidates[0]

    if not target or "webSocketDebuggerUrl" not in target:
        raise RuntimeError(
            "No controlled Colab tab found. Call open_colab_browser_connection first."
        )
    return target["webSocketDebuggerUrl"]


def _evaluate_colab_page(expression: str, *, await_promise: bool = False):
    return _edge_cdp_eval(
        _colab_page_websocket_url(), expression, await_promise=await_promise
    )


def _strip_ansi_sequences(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b[@-Z\\-_]|\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return text


def _terminal_endpoint_info() -> dict:
    expression = r"""
JSON.stringify((()=>{
  const body = document.body?.innerText || '';
  const email = window.colabUserEmail || null;
  if (!email || email === 'anonymous' || /Sign in|登录|登入/.test(body)) {
    return {ok:false, loginRequired:true, message:'Colab login is required in the dedicated MCP browser. Ask the user to log into Google/Colab in that Edge window, then rerun the command.'};
  }
  const notebook = window.colab?.global?.notebook;
  const kernel = notebook?.kernel || notebook?.getKernel?.();
  const runtime = kernel?.runtime;
  if (!kernel || !runtime) {
    return {ok:false, runtimeDisconnected:true, error:'Colab runtime is not connected yet; reconnect the runtime and retry.'};
  }
  let socket = null;
  try {
    socket = kernel?.createTerminalSocket?.(runtime);
  } catch (error) {
    return {ok:false, runtimeDisconnected:true, error:String(error?.message || error)};
  }
  const token = socket?.getRuntimeProxyToken?.();
  if (!socket?.url || !token) {
    return {ok:false, error:'Colab runtime terminal endpoint is not available or the runtime is disconnected.'};
  }
  return {ok:true, url:socket.url, token};
})())
"""
    evaluation = _evaluate_colab_page(expression)
    if "exceptionDetails" in evaluation:
        raise RuntimeError(
            json.dumps(evaluation["exceptionDetails"], ensure_ascii=False)
        )
    value = evaluation.get("result", {}).get("value")
    if not isinstance(value, str):
        raise RuntimeError("Could not read Colab terminal endpoint from the browser.")
    info = json.loads(value)
    if not info.get("ok"):
        raise RuntimeError(
            info.get("message")
            or info.get("error")
            or "Colab terminal endpoint is not available."
        )
    return info


def _terminal_websocket_url(info: dict) -> str:
    parsed = urllib.parse.urlparse(info["url"])
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "colab-runtime-proxy-token"]
    query.append(("colab-runtime-proxy-token", info["token"]))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def _runtime_base_url(info: dict) -> str:
    parsed = urllib.parse.urlparse(info["url"])
    return urllib.parse.urlunparse(
        parsed._replace(
            scheme="https" if parsed.scheme == "wss" else "http",
            path="",
            params="",
            query="",
            fragment="",
        )
    ).rstrip("/")


def _read_runtime_text_file(path: str) -> str:
    info = _terminal_endpoint_info()
    api_path = urllib.parse.quote(path.lstrip("/"), safe="/")
    url = (
        f"{_runtime_base_url(info)}/api/contents/{api_path}?"
        + urllib.parse.urlencode(
            {"content": "1", "colab-runtime-proxy-token": info["token"]}
        )
    )
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload.get("content", "")
    if payload.get("format") == "base64":
        return base64.b64decode(content).decode("utf-8")
    return str(content)


def _runtime_contents_url(path: str, *, content: bool | None = None) -> str:
    info = _terminal_endpoint_info()
    api_path = urllib.parse.quote(path.lstrip("/"), safe="/")
    query = {"colab-runtime-proxy-token": info["token"]}
    if content is not None:
        query["content"] = "1" if content else "0"
    return f"{_runtime_base_url(info)}/api/contents/{api_path}?{urllib.parse.urlencode(query)}"


def _runtime_files_url(path: str) -> str:
    info = _terminal_endpoint_info()
    api_path = urllib.parse.quote(path.lstrip("/"), safe="/")
    query = {"colab-runtime-proxy-token": info["token"]}
    return f"{_runtime_base_url(info)}/files/{api_path}?{urllib.parse.urlencode(query)}"


def _runtime_contents_request(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    content: bool | None = None,
) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(
        _runtime_contents_url(path, content=content),
        method=method,
        data=data,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _runtime_parent_dirs(path: str) -> list[str]:
    parts = [part for part in path.strip("/").split("/")[:-1] if part]
    dirs = []
    for index in range(1, len(parts) + 1):
        dirs.append("/" + "/".join(parts[:index]))
    return dirs


def _runtime_file_parent_and_name(path: str) -> tuple[str, str]:
    clean = "/" + path.strip("/")
    parent, name = clean.rsplit("/", 1)
    if not name:
        raise ValueError(f"Expected a file path, got directory path: {path}")
    return parent.lstrip("/"), name


def _runtime_browser_write_file(path: str, data: bytes) -> dict:
    parent, name = _runtime_file_parent_and_name(path)
    content_base64 = base64.b64encode(data).decode("ascii")
    script = f"""
(async () => {{
  const nb = window.colab?.global?.notebook;
  const kf = nb?.kernelFiles;
  const body = document.body?.innerText || '';
  const email = window.colabUserEmail || null;
  const loginRequired = !email || email === 'anonymous' || /Sign in|\\u767b\\u5f55|\\u767b\\u5165/.test(body);
  if (loginRequired) return JSON.stringify({{ok:false, loginRequired:true, message:'Colab login is required in the dedicated MCP browser. Ask the user to log into Google/Colab in that Edge window, then rerun the command.'}});
  if (!kf) return JSON.stringify({{ok:false, error:'Colab kernel file API is unavailable. Connect the runtime and rerun the command.'}});
  const raw = atob({json.dumps(content_base64)});
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const response = await kf.write({json.dumps(parent)}, {json.dumps(name)}, bytes.buffer, {{canceled:false, update:()=>{{}}}});
  return JSON.stringify({{ok: !!response?.ok, status: response?.status || null, statusText: response?.statusText || '', path: {json.dumps("/" + path.strip("/"))}}});
}})()
"""
    evaluation = _evaluate_colab_page(script, await_promise=True)
    if "exceptionDetails" in evaluation:
        raise RuntimeError(
            json.dumps(evaluation["exceptionDetails"], ensure_ascii=False)
        )
    raw_value = evaluation.get("result", {}).get("value")
    result = json.loads(raw_value) if isinstance(raw_value, str) else {}
    if result.get("loginRequired"):
        raise RuntimeError(result["message"])
    if not result.get("ok"):
        raise RuntimeError(
            result.get("error")
            or f"Colab file upload API failed with status {result.get('status')}: {result.get('statusText')}"
        )
    return result


def _resolve_local_path(path: str, *, purpose: str) -> Path:
    if not path or not str(path).strip():
        raise ValueError(f"Local path is required for {purpose}.")
    return Path(path).expanduser().resolve()


def _runtime_download_bytes(
    path: str, *, offset: int = 0, max_bytes: int | None = None
) -> tuple[bytes, int]:
    headers = {}
    if offset or max_bytes is not None:
        end = "" if max_bytes is None else str(offset + max_bytes - 1)
        headers["Range"] = f"bytes={offset}-{end}"
    request = urllib.request.Request(_runtime_files_url(path), headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
        size_header = response.headers.get("X-File-Size")
        content_range = response.headers.get("Content-Range")
    size = int(size_header) if size_header and size_header.isdigit() else None
    if size is None and content_range:
        match = re.search(r"/(\d+)$", content_range)
        if match:
            size = int(match.group(1))
    return data, size if size is not None else offset + len(data)


def _runtime_make_directory(
    path: str, *, parents: bool = True, exist_ok: bool = True
) -> dict:
    targets = _runtime_parent_dirs(path + "/child") if parents else [path]
    last = None
    for target in targets:
        try:
            last = _runtime_contents_request(
                target, method="PUT", body={"type": "directory"}
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and exist_ok:
                last = _runtime_contents_request(target, content=False)
                continue
            raise
    return {
        "path": path,
        "created": True,
        "exists": True,
        "apiPath": (last or {}).get("path"),
    }


def _runtime_upload_file(
    path: str,
    content_base64: str,
    *,
    overwrite: bool = False,
    make_parents: bool = True,
) -> dict:
    if make_parents:
        for parent in _runtime_parent_dirs(path):
            with contextlib.suppress(urllib.error.HTTPError):
                _runtime_contents_request(
                    parent, method="PUT", body={"type": "directory"}
                )
    if not overwrite:
        with contextlib.suppress(urllib.error.HTTPError):
            _runtime_contents_request(path, content=False)
            raise FileExistsError(path)
    data = base64.b64decode(content_base64)
    _runtime_browser_write_file(path, data)
    return {
        "path": path,
        "size": len(data),
        "sha256": __import__("hashlib").sha256(data).hexdigest(),
    }


def _runtime_upload_local_file(
    local_path: str,
    path: str,
    *,
    overwrite: bool = False,
    make_parents: bool = True,
) -> dict:
    import hashlib

    source = _resolve_local_path(local_path, purpose="upload")
    if not source.exists():
        raise FileNotFoundError(f"Local file not found: {source}")
    if not source.is_file():
        raise ValueError(f"Expected a local file path, got: {source}")
    data = source.read_bytes()
    result = _runtime_upload_file(
        path,
        base64.b64encode(data).decode("ascii"),
        overwrite=overwrite,
        make_parents=make_parents,
    )
    result.update(
        {
            "localPath": str(source),
            "localSize": len(data),
            "localSha256": hashlib.sha256(data).hexdigest(),
        }
    )
    return result


def _runtime_content_to_bytes(payload: dict) -> bytes:
    content = payload.get("content", "")
    if payload.get("format") == "base64":
        return base64.b64decode(content)
    return str(content).encode("utf-8")


def _runtime_download_file(
    path: str, *, offset: int = 0, max_bytes: int | None = None
) -> dict:
    chunk, size = _runtime_download_bytes(path, offset=offset, max_bytes=max_bytes)
    return {
        "path": path,
        "offset": offset,
        "size": size,
        "readBytes": len(chunk),
        "contentBase64": base64.b64encode(chunk).decode("ascii"),
        "sha256": __import__("hashlib").sha256(chunk).hexdigest(),
    }


def _runtime_download_file_to_local(
    path: str,
    local_path: str,
    *,
    overwrite: bool = False,
    make_parents: bool = True,
) -> dict:
    import hashlib

    target = _resolve_local_path(local_path, purpose="download")
    if target.exists() and not overwrite:
        raise FileExistsError(f"Local file already exists: {target}")
    if make_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    chunk, size = _runtime_download_bytes(path)
    target.write_bytes(chunk)
    digest = hashlib.sha256(chunk).hexdigest()
    return {
        "path": path,
        "localPath": str(target),
        "size": size,
        "writtenBytes": len(chunk),
        "sha256": digest,
    }


def _runtime_download_file_chunk(
    path: str, *, offset: int = 0, max_bytes: int = 1048576
) -> dict:
    chunk, size = _runtime_download_bytes(path, offset=offset, max_bytes=max_bytes)
    next_offset = offset + len(chunk)
    return {
        "path": path,
        "offset": offset,
        "nextOffset": next_offset,
        "size": size,
        "done": next_offset >= size,
        "readBytes": len(chunk),
        "contentBase64": base64.b64encode(chunk).decode("ascii"),
        "chunkSha256": __import__("hashlib").sha256(chunk).hexdigest(),
    }


def _runtime_stat_file(path: str) -> dict:
    payload = _runtime_contents_request(path, content=False) or {}
    result = {
        "path": "/" + payload.get("path", path).lstrip("/"),
        "exists": True,
        "isFile": payload.get("type") == "file",
        "isDir": payload.get("type") == "directory",
        "size": payload.get("size") or 0,
        "mtime": payload.get("last_modified"),
        "mode": payload.get("type"),
    }
    if result["isFile"]:
        data, _ = _runtime_download_bytes(path)
        result["sha256"] = __import__("hashlib").sha256(data).hexdigest()
    return result


def _runtime_list_files(
    path: str = ".", *, recursive: bool = False, max_entries: int = 1000
) -> dict:
    root = (
        path
        if path.startswith("/")
        else f"/content/{path}"
        if path != "."
        else "/content"
    )
    entries = []

    def visit(current: str):
        nonlocal entries
        payload = _runtime_contents_request(current, content=True) or {}
        for item in payload.get("content") or []:
            item_path = "/" + item.get("path", "").lstrip("/")
            entry = {
                "path": item_path,
                "name": item.get("name"),
                "isFile": item.get("type") == "file",
                "isDir": item.get("type") == "directory",
                "size": item.get("size") or 0,
                "mtime": item.get("last_modified"),
            }
            entries.append(entry)
            if len(entries) >= max_entries:
                return
            if recursive and entry["isDir"]:
                visit(item_path)
                if len(entries) >= max_entries:
                    return

    visit(root)
    return {
        "path": root,
        "recursive": recursive,
        "entries": entries,
        "truncated": len(entries) >= max_entries,
    }


def _runtime_delete_file(
    path: str, *, recursive: bool = False, force: bool = False
) -> dict:
    real_path = "/" + path.lstrip("/")
    allowed = (
        real_path == "/content"
        or real_path.startswith("/content/")
        or real_path == "/tmp"
        or real_path.startswith("/tmp/")
    )
    if not force and not allowed:
        raise PermissionError(
            f"Refusing to delete outside /content or /tmp without force=true: {real_path}"
        )
    if recursive:
        with contextlib.suppress(Exception):
            for entry in sorted(
                _runtime_list_files(real_path, recursive=True, max_entries=100000)[
                    "entries"
                ],
                key=lambda item: item["path"].count("/"),
                reverse=True,
            ):
                _runtime_contents_request(entry["path"], method="DELETE")
    _runtime_contents_request(real_path, method="DELETE")
    return {"path": real_path, "deleted": True}


def _runtime_upload_file_chunk(
    upload_id: str,
    path: str,
    chunk_base64: str,
    *,
    chunk_index: int,
    overwrite: bool = False,
) -> dict:
    import hashlib

    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", upload_id).strip("_") or "upload"
    tmp_path = f"/content/.colab_mcp_upload_{safe_id}.part"
    existing = b""
    if chunk_index != 0:
        with contextlib.suppress(Exception):
            existing, _ = _runtime_download_bytes(tmp_path)
    elif not overwrite:
        with contextlib.suppress(urllib.error.HTTPError):
            _runtime_contents_request(path, content=False)
            raise FileExistsError(path)
    data = base64.b64decode(chunk_base64)
    combined = existing + data
    _runtime_upload_file(
        tmp_path,
        base64.b64encode(combined).decode("ascii"),
        overwrite=True,
        make_parents=True,
    )
    return {
        "uploadId": upload_id,
        "path": path,
        "tmpPath": tmp_path,
        "chunkIndex": chunk_index,
        "chunkBytes": len(data),
        "tmpSize": len(combined),
        "chunkSha256": hashlib.sha256(data).hexdigest(),
    }


def _runtime_complete_upload(
    upload_id: str, path: str, *, overwrite: bool = False
) -> dict:
    import hashlib

    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", upload_id).strip("_") or "upload"
    tmp_path = f"/content/.colab_mcp_upload_{safe_id}.part"
    data, _ = _runtime_download_bytes(tmp_path)
    _runtime_upload_file(
        path,
        base64.b64encode(data).decode("ascii"),
        overwrite=overwrite,
        make_parents=True,
    )
    _runtime_contents_request(tmp_path, method="DELETE")
    return {
        "uploadId": upload_id,
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _terminal_message_data(message) -> tuple[str, bool]:
    if isinstance(message, bytes):
        return message.decode("utf-8", errors="replace"), False
    try:
        parsed = json.loads(message)
    except Exception:
        return str(message), False
    data = parsed.get("data")
    if data is None:
        return "", bool(parsed.get("ack"))
    if isinstance(data, str):
        return data, bool(parsed.get("ack"))
    if isinstance(data, list):
        return bytes(data).decode("utf-8", errors="replace"), bool(parsed.get("ack"))
    return str(data), bool(parsed.get("ack"))


def _run_direct_terminal_command(
    command: str, *, marker: str, timeout_seconds: int
) -> str:
    info = _terminal_endpoint_info()
    websocket_url = _terminal_websocket_url(info)
    output = ""
    deadline = time.monotonic() + timeout_seconds
    with websocket_connect(
        websocket_url,
        open_timeout=10,
        ping_interval=None,
        max_size=None,
    ) as ws:
        ws.send(json.dumps({"cols": 320, "rows": 200}))
        ws.send(json.dumps({"data": "stty -echo\r"}))
        quiet_until = min(deadline, time.monotonic() + 0.3)
        while time.monotonic() < quiet_until:
            try:
                ws.recv(timeout=0.1)
            except TimeoutError:
                break
        ws.send(json.dumps({"data": command + "; stty echo\r"}))
        while time.monotonic() < deadline:
            try:
                message = ws.recv(
                    timeout=min(1.0, max(0.1, deadline - time.monotonic()))
                )
            except TimeoutError:
                continue
            data, ack = _terminal_message_data(message)
            if ack:
                ws.send(json.dumps({"ack": True}))
            output += data
            if len(output) > 2_000_000:
                output = output[-2_000_000:]
            clean = _strip_ansi_sequences(output)
            if f"{marker}RESULT{marker}" in clean or f"{marker}END{marker}" in clean:
                return output
        return output


def _terminal_command_expression(
    command: str, *, marker: str, timeout_seconds: int
) -> str:
    command_json = json.dumps(command)
    marker_json = json.dumps(marker)
    timeout_ms = max(1, int(timeout_seconds)) * 1000
    return f"""
(async()=>{{
  const command = {command_json};
  const marker = {marker_json};
  const timeoutMs = {timeout_ms};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const body = document.body?.innerText || '';
  const email = window.colabUserEmail || null;
  if (!email || email === 'anonymous' || /Sign in|登录|登入/.test(body)) {{
    return JSON.stringify({{
      ok: false,
      loginRequired: true,
      message: 'Colab login is required in the dedicated MCP browser. Ask the user to log into Google/Colab in that Edge window, then rerun the command.'
    }});
  }}
  const notebook = window.colab?.global?.notebook;
  const kernel = notebook?.kernel || notebook?.getKernel?.();
  const runtime = kernel?.runtime;
  const terminalConnection = kernel?.terminalConnection || kernel?.getTerminalConnection?.();
  if (!kernel || !runtime || !terminalConnection) {{
    return JSON.stringify({{ok:false,error:'Colab runtime terminal is not available or the runtime is disconnected.'}});
  }}
  let socket = null;
  try {{
    socket = terminalConnection.currentConnection ? await terminalConnection.currentConnection.catch(() => null) : null;
  }} catch (e) {{
    socket = null;
  }}
  if (!socket || !socket.isOpen?.()) {{
    try {{ terminalConnection.disconnect?.(); }} catch (e) {{}}
    socket = await terminalConnection.connect(runtime);
  }}
  if (!socket?.isOpen?.()) {{
    return JSON.stringify({{ok:false,error:'Colab terminal websocket did not open.'}});
  }}
  let output = '';
  let sawMarker = false;
  const append = value => {{
    try {{
      let data = value;
      if (typeof data === 'string') {{
        try {{
          const parsed = JSON.parse(data);
          if (parsed?.data == null) return;
          data = parsed.data;
          if (parsed.ack) socket.sendString(JSON.stringify({{ack:true}}));
        }} catch (e) {{}}
      }}
      if (typeof data !== 'string') return;
      output += data;
      if (output.length > 1000000) output = output.slice(-1000000);
      if (output.includes(marker)) sawMarker = true;
    }} catch (e) {{}}
  }};
  const onMessage = ev => append(ev.data);
  socket.webSocket?.addEventListener('message', onMessage);
  try {{
    const cols = Math.max(window.colab?.global?.notebook?.terminal?.terminal?.cols || 80, 200);
    const rows = Math.max(window.colab?.global?.notebook?.terminal?.terminal?.rows || 24, 40);
    socket.sendString(JSON.stringify({{cols, rows}}));
    await sleep(100);
    socket.sendString(JSON.stringify({{data: command + '\\r'}}));
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {{
      if (sawMarker) {{
        await sleep(150);
        if ((output.match(new RegExp(marker.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&'), 'g')) || []).length >= 2) break;
      }}
      await sleep(100);
    }}
    return JSON.stringify({{
      ok: sawMarker,
      timedOut: !sawMarker,
      executionBackend: 'colab-terminal',
      output
    }});
  }} finally {{
    socket.webSocket?.removeEventListener('message', onMessage);
  }}
}})()
"""


def _runtime_accelerator_state_expression() -> str:
    return r"""
(() => {
  function allDeep(root = document) {
    const nodes = [];
    const walk = node => {
      if (!node) return;
      if (node.nodeType === Node.ELEMENT_NODE) {
        nodes.push(node);
        if (node.shadowRoot) walk(node.shadowRoot);
      }
      for (const child of node.children || []) walk(child);
    };
    walk(root);
    return nodes;
  }
  function visible(el) {
    const rect = el.getBoundingClientRect?.();
    return Boolean(rect && rect.width > 0 && rect.height > 0);
  }
  function label(el) {
    return (
      el.getAttribute?.('aria-label') ||
      el.getAttribute?.('title') ||
      el.innerText ||
      el.textContent ||
      ''
    ).trim();
  }
  const body = document.body?.innerText || '';
  const options = allDeep()
    .filter(el => visible(el))
    .map(el => ({
      tag: el.tagName,
      role: el.getAttribute?.('role') || null,
      text: label(el).slice(0, 160),
      checked: Boolean(el.checked),
    }))
    .filter(item => /CPU|GPU|TPU|Hardware accelerator|Accelerator|硬件加速器|运行时类型|Runtime type/i.test(item.text))
    .slice(0, 100);
  return JSON.stringify({
    ok: true,
    href: location.href,
    ready: document.readyState,
    hasGpuHint: /\b(T4|L4|A100|H100|G4|GPU|TPU)\b/i.test(body),
    options,
  });
})()
"""


def _runtime_connection_expression(wait_seconds: int) -> str:
    wait_ms = max(1, min(int(wait_seconds), 300)) * 1000
    return rf"""
(async () => {{
  const waitMs = {wait_ms};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {{ ok: false, steps: [], warnings: [] }};
  const connectLabels = [
    'Connect to a new runtime',
    'Connect to hosted runtime',
    'Reconnect',
    'Connect',
    '\u8fde\u63a5\u5230\u65b0\u7684\u8fd0\u884c\u65f6',
    '\u8fde\u63a5\u5230\u6258\u7ba1\u7684\u8fd0\u884c\u65f6',
    '\u91cd\u65b0\u8fde\u63a5',
    '\u8fde\u63a5'
  ];
  const confirmLabels = [
    'Connect anyway',
    'OK',
    'Yes',
    'Run anyway',
    '\u786e\u5b9a',
    '\u662f'
  ];

  function allDeep(root = document) {{
    const nodes = [];
    const walk = node => {{
      if (!node) return;
      if (node.nodeType === Node.ELEMENT_NODE) {{
        nodes.push(node);
        if (node.shadowRoot) walk(node.shadowRoot);
      }}
      for (const child of node.children || []) walk(child);
    }};
    walk(root);
    return nodes;
  }}
  function visible(el) {{
    const rect = el.getBoundingClientRect?.();
    return Boolean(rect && rect.width > 0 && rect.height > 0);
  }}
  function label(el) {{
    return (
      el.getAttribute?.('aria-label') ||
      el.getAttribute?.('title') ||
      el.innerText ||
      el.textContent ||
      ''
    ).trim();
  }}
  function interactive(el) {{
    const tag = el.tagName || '';
    const role = el.getAttribute?.('role') || '';
    return tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag.startsWith('MD-') ||
      tag.startsWith('MWC-') || tag.startsWith('PAPER-') ||
      ['button', 'menuitem', 'option', 'link'].includes(role);
  }}
  function clickElement(el) {{
    el.scrollIntoView?.({{ block: 'center', inline: 'center' }});
    const rect = el.getBoundingClientRect?.();
    const x = rect ? rect.left + rect.width / 2 : 0;
    const y = rect ? rect.top + rect.height / 2 : 0;
    for (const type of ['pointerover', 'mouseover', 'pointermove', 'mousemove', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
      el.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }}));
    }}
    el.click?.();
    return {{ clicked: true, tag: el.tagName, role: el.getAttribute?.('role') || null, text: label(el).slice(0, 200) }};
  }}
  function runtimeState() {{
    const body = document.body?.innerText || '';
    const email = window.colabUserEmail || null;
    const notebook = window.colab?.global?.notebook;
    const service = notebook?.localColabMcpService;
    const kernel = notebook?.kernel || notebook?.getKernel?.();
    const runtime = kernel?.runtime;
    const terminalConnection = kernel?.terminalConnection || kernel?.getTerminalConnection?.();
    const relevant = allDeep()
      .filter(el => visible(el))
      .map(el => ({{ tag: el.tagName, role: el.getAttribute?.('role') || null, text: label(el).slice(0, 180) }}))
      .filter(item => /Connect|Reconnect|runtime|运行时|连接|重新连接|GPU|TPU/i.test(item.text))
      .slice(0, 80);
    return {{
      href: location.href,
      ready: document.readyState,
      loginRequired: !email || email === 'anonymous' || /Sign in|\u767b\u5f55|\u767b\u5165/.test(body),
      localMcpConnected: Boolean(service?.isConnected?.()),
      hasNotebook: Boolean(notebook),
      hasKernel: Boolean(kernel),
      hasRuntime: Boolean(runtime),
      hasTerminalConnection: Boolean(terminalConnection),
      terminalReady: Boolean(runtime && (kernel?.createTerminalSocket || terminalConnection)),
      kernelLastConnectedTimeMs: notebook?.kernelLastConnectedTimeMs ?? null,
      relevant,
    }};
  }}
  function candidateScore(el, needle) {{
    const text = label(el).toLowerCase();
    const exact = text === needle ? 80 : 0;
    const prefix = text.startsWith(needle) ? 30 : 0;
    const interact = interactive(el) ? 20 : 0;
    const tag = el.tagName || '';
    const material = tag.startsWith('MD-') || tag.startsWith('MWC-') || tag.startsWith('PAPER-') ? 8 : 0;
    return exact + prefix + interact + material - Math.min(text.length / 50, 10);
  }}
  function findByLabels(labels) {{
    const candidates = allDeep().filter(el => visible(el));
    for (const raw of labels) {{
      const needle = String(raw).toLowerCase();
      const ranked = candidates
        .map(el => {{
          const target = interactive(el)
            ? el
            : el.closest?.('button,[role=button],md-text-button,mwc-button,paper-button');
          if (!target || !visible(target)) return null;
          const own = label(el);
          const targetText = label(target);
          const text = `${{own}}\n${{targetText}}`.toLowerCase();
          return {{ el: target, text, score: candidateScore(target, needle) + candidateScore(el, needle) }};
        }})
        .filter(Boolean)
        .filter(item => item.text && item.text.length <= 500 && item.text.includes(needle))
        .sort((a, b) => b.score - a.score);
      if (ranked[0]?.el) return ranked[0].el;
    }}
    return null;
  }}
  function clickByLabels(labels) {{
    const direct = findByLabels(labels);
    if (!direct) return {{ clicked: false, reason: 'not found', labels }};
    const target = interactive(direct)
      ? direct
      : direct.closest?.('button,[role=button],md-text-button,mwc-button,paper-button') || direct;
    return clickElement(target);
  }}
  async function closeLocalMcpDialog() {{
    const closed = [];
    for (let i = 0; i < 3; i++) {{
      const dialog = allDeep()
        .filter(el => visible(el) && ['dialog', 'alertdialog'].includes(el.getAttribute?.('role') || ''))
        .find(el => /local Colab MCP server|\u672c\u5730 Colab MCP/i.test(label(el)));
      if (!dialog) break;
      const button = allDeep(dialog)
        .filter(el => visible(el) && interactive(el))
        .find(el => /^(Connect|\u8fde\u63a5)$/.test(label(el)));
      if (!button) break;
      closed.push(clickElement(button));
      await sleep(1000);
    }}
    return closed;
  }}
  function clickRuntimeToolbar() {{
    const toolbar = allDeep()
      .filter(el => visible(el) && el.tagName === 'COLAB-TOOLBAR-BUTTON')
      .find(el => /^(Connect|Reconnect|\u8fde\u63a5|\u91cd\u65b0\u8fde\u63a5)(\s|$)/i.test(label(el)));
    if (!toolbar) return {{ clicked: false, reason: 'runtime toolbar not found' }};
    return clickElement(toolbar);
  }}
  async function connectLocalMcp() {{
    const service = window.colab?.global?.notebook?.localColabMcpService;
    if (!service?.isConnected?.() && service?.connect) {{
      if (service.colabMcpServer?.closeServer) {{
        try {{ await service.colabMcpServer.closeServer(); }} catch (e) {{}}
      }}
      await Promise.race([
        service.connect(),
        new Promise((_, reject) => setTimeout(() => reject(new Error('localColabMcpService.connect timed out')), 20000)),
      ]);
    }}
  }}
  async function clickConfirmDialogs() {{
    const clicked = [];
    for (let i = 0; i < 8; i++) {{
      await sleep(750);
      if (runtimeState().terminalReady) break;
      const step = clickByLabels(confirmLabels);
      if (step.clicked) clicked.push(step);
    }}
    return clicked;
  }}

  out.before = runtimeState();
  if (out.before.loginRequired) {{
    out.warnings.push('Colab login is required in the dedicated MCP browser before connecting a runtime.');
    out.after = out.before;
    return JSON.stringify(out);
  }}
  if (!out.before.localMcpConnected) {{
    try {{
      await connectLocalMcp();
      await sleep(1000);
    }} catch (error) {{
      await sleep(5000);
      try {{
        await connectLocalMcp();
        await sleep(1000);
      }} catch (err2) {{
        out.warnings.push(`Local MCP reconnect attempt failed: ${{String(err2?.message || err2)}}`);
      }}
    }}
  }}
  out.localMcpDialogClosed = await closeLocalMcpDialog();

  let current = runtimeState();
  if (current.terminalReady) {{
    out.ok = true;
    out.after = current;
    return JSON.stringify(out);
  }}

  let step = clickRuntimeToolbar();
  out.steps.push({{ name: 'click-runtime-toolbar', ...step }});
  await sleep(1000);
  current = runtimeState();
  if (current.terminalReady) {{
    out.ok = true;
    out.after = current;
    return JSON.stringify(out);
  }}

  step = clickByLabels(connectLabels);
  out.steps.push({{ name: 'click-connect-runtime', ...step }});
  await sleep(1200);
  out.confirmations = await clickConfirmDialogs();

  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {{
    current = runtimeState();
    if (current.terminalReady) {{
      out.ok = true;
      out.after = current;
      return JSON.stringify(out);
    }}
    if (current.loginRequired) {{
      out.warnings.push('Colab login became required while waiting for runtime connection.');
      break;
    }}
    await sleep(2000);
  }}

  out.after = runtimeState();
  if (!out.steps.some(s => s.clicked)) {{
    out.warnings.push('No visible Connect/Reconnect runtime control was clicked.');
  }}
  if (!out.after.terminalReady) {{
    out.warnings.push('Timed out waiting for a real Colab runtime. Terminal-backed tools require kernel.runtime and a terminal socket.');
  }}
  return JSON.stringify(out);
}})()
"""


def _runtime_accelerator_expression(accelerator: str, apply: bool) -> str:
    accelerator_json = json.dumps(accelerator, ensure_ascii=False)
    apply_json = "true" if apply else "false"
    return rf"""
(async () => {{
  const accelerator = {accelerator_json};
  const shouldApply = {apply_json};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const out = {{ ok: false, accelerator, applied: false, steps: [], warnings: [] }};
  const runtimeMenuLabels = ['Runtime', 'Code execution program', '\u4ee3\u7801\u6267\u884c\u7a0b\u5e8f'];
  const runtimeDialogLabels = ['Change runtime type', 'Runtime type', '\u66f4\u6539\u8fd0\u884c\u65f6\u7c7b\u578b', '\u8fd0\u884c\u65f6\u7c7b\u578b'];
  const saveLabels = ['Save', 'Apply', '\u4fdd\u5b58'];
  const confirmLabels = ['OK', 'Yes', 'Restart runtime', '\u786e\u5b9a', '\u662f'];
  const targetLabels = accelerator.toLowerCase() === 'gpu'
    ? ['T4 GPU', 'L4 GPU', 'A100 GPU', 'H100 GPU', 'G4 GPU', 'GPU']
    : accelerator.toLowerCase() === 'none'
    ? ['None', 'No accelerator', 'CPU']
    : [accelerator, accelerator.toUpperCase(), accelerator.toLowerCase()];

  function allDeep(root = document) {{
    const nodes = [];
    const walk = node => {{
      if (!node) return;
      if (node.nodeType === Node.ELEMENT_NODE) {{
        nodes.push(node);
        if (node.shadowRoot) walk(node.shadowRoot);
      }}
      for (const child of node.children || []) walk(child);
    }};
    walk(root);
    return nodes;
  }}
  function visible(el) {{
    const rect = el.getBoundingClientRect?.();
    return Boolean(rect && rect.width > 0 && rect.height > 0);
  }}
  function label(el) {{
    return (
      el.getAttribute?.('aria-label') ||
      el.getAttribute?.('title') ||
      el.innerText ||
      el.textContent ||
      ''
    ).trim();
  }}
  function interactive(el) {{
    const tag = el.tagName || '';
    const role = el.getAttribute?.('role') || '';
    return tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag.startsWith('MD-') ||
      tag.startsWith('MWC-') || tag.startsWith('PAPER-') ||
      ['button', 'menuitem', 'option', 'link', 'combobox', 'radio'].includes(role);
  }}
  function clickElement(el) {{
    el.scrollIntoView?.({{ block: 'center', inline: 'center' }});
    const rect = el.getBoundingClientRect?.();
    const x = rect ? rect.left + rect.width / 2 : 0;
    const y = rect ? rect.top + rect.height / 2 : 0;
    for (const type of ['pointerover', 'mouseover', 'pointermove', 'mousemove', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
      el.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }}));
    }}
    el.click?.();
    return {{ clicked: true, tag: el.tagName, role: el.getAttribute?.('role') || null, text: label(el).slice(0, 200) }};
  }}
  function findByLabels(labels, exact = false) {{
    const candidates = allDeep().filter(el => visible(el));
    for (const raw of labels) {{
      const needle = String(raw).toLowerCase();
      const ranked = candidates
        .map(el => {{
          const text = label(el).toLowerCase();
          return {{
            el,
            text,
            score: (el.tagName === 'INPUT' ? 8 : 0) + (el.tagName === 'LABEL' ? 4 : 0) + (interactive(el) ? 2 : 0),
          }};
        }})
        .filter(item => item.text && (exact ? item.text === needle : item.text.includes(needle)))
        .sort((a, b) => b.score - a.score);
      if (ranked[0]?.el) return ranked[0].el;
    }}
    return null;
  }}
  function clickByLabels(labels, exact = false) {{
    const direct = findByLabels(labels, exact);
    if (!direct) return {{ clicked: false, reason: 'not found', labels }};
    if (interactive(direct)) return clickElement(direct);
    const ancestor = direct.closest?.('button,[role=button],[role=menuitem],[role=option],[role=radio],input,md-radio,md-text-button,md-menu-item,mwc-button,paper-button') || direct;
    return clickElement(ancestor);
  }}
  function state() {{
    const body = document.body?.innerText || '';
    const relevant = allDeep()
      .filter(el => visible(el))
      .map(el => ({{ tag: el.tagName, role: el.getAttribute?.('role') || null, text: label(el).slice(0, 180), checked: Boolean(el.checked) }}))
      .filter(item => /Runtime type|Hardware accelerator|CPU|GPU|TPU|Save|Apply|Restart|\u8fd0\u884c\u65f6|\u786c\u4ef6\u52a0\u901f|\u4fdd\u5b58|\u91cd\u542f/i.test(item.text))
      .slice(0, 100);
    return {{
      href: location.href,
      hasGpuHint: /\b(T4|L4|A100|H100|G4|GPU|TPU)\b/i.test(body),
      relevant,
    }};
  }}
  function closeMenusAndDialogs() {{
    document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', bubbles: true }}));
    document.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'Escape', bubbles: true }}));
  }}
  async function clickConfirmDialogs() {{
    const clicked = [];
    for (let i = 0; i < 6; i++) {{
      await sleep(700);
      const button = allDeep()
        .filter(el => visible(el) && confirmLabels.includes(label(el)))
        .pop();
      if (!button) continue;
      clicked.push(clickElement(button));
    }}
    return clicked;
  }}

  closeMenusAndDialogs();
  await sleep(400);
  out.before = state();
  let step = clickByLabels(runtimeMenuLabels);
  out.steps.push({{ name: 'open-runtime-menu', ...step }});
  await sleep(700);
  step = clickByLabels(runtimeDialogLabels);
  out.steps.push({{ name: 'open-runtime-dialog', ...step }});
  await sleep(1400);
  step = clickByLabels(targetLabels, true);
  if (!step.clicked) step = clickByLabels(targetLabels);
  out.steps.push({{ name: 'choose-accelerator', ...step }});
  await sleep(600);
  out.dialogState = state();

  if (shouldApply) {{
    step = clickByLabels(saveLabels, true);
    if (!step.clicked) step = clickByLabels(saveLabels);
    out.steps.push({{ name: 'apply-runtime-dialog', ...step }});
    out.applied = Boolean(step.clicked);
    out.confirmations = await clickConfirmDialogs();
    out.warnings.push('Changing accelerator can restart or disconnect the Colab runtime; reconnect before running runtime commands.');
  }}

  out.after = state();
  const afterText = JSON.stringify(out.after);
  if (/无法保存更改|Unable to save|cloud_off/.test(afterText)) {{
    out.warnings.push('Colab reported that notebook changes could not be saved; the accelerator selection may still require a runtime reconnect or a Drive copy.');
  }}
  if (/连接到新的运行时|Connect to a new runtime/.test(afterText)) {{
    out.warnings.push('Colab is not attached to a runtime after the accelerator change; connect the runtime before running GPU checks.');
  }}
  const failed = out.steps.filter(s => !s.clicked && !['apply-runtime-dialog'].includes(s.name));
  out.ok = failed.length === 0 && (!shouldApply || out.applied);
  if (failed.length) out.warnings.push(`Some UI steps did not click: ${{failed.map(s => s.name).join(', ')}}`);
  return JSON.stringify(out);
}})()
"""


def _navigate_controlled_edge(
    url: str, *, token: str | None = None, mcp_port: int | str | None = None
) -> bool:
    port = os.environ.get(EDGE_CDP_PORT_ENV, DEFAULT_EDGE_CDP_PORT)
    url_contains = os.environ.get(
        EDGE_CDP_URL_CONTAINS_ENV, "colab.research.google.com"
    )
    try:
        _ensure_controlled_edge(url, port=port)
        pages = [
            tab
            for tab in _cdp_json(f"http://127.0.0.1:{port}/json/list")
            if tab.get("type") == "page"
        ]
        candidates = [tab for tab in pages if url_contains in tab.get("url", "")]
        target = None
        clean_url = url.split("#")[0].split("?")[0]
        if clean_url and not clean_url.endswith("empty.ipynb"):
            target = next(
                (tab for tab in candidates if clean_url in tab.get("url", "")), None
            )
        if target is None:
            target = next(
                (tab for tab in candidates if "empty.ipynb" not in tab.get("url", "")),
                None,
            )
        if target is None and candidates:
            target = candidates[0]
        if target is None:
            encoded_url = urllib.parse.quote(url, safe="")
            target = _cdp_json(
                f"http://127.0.0.1:{port}/json/new?{encoded_url}", method="PUT"
            )
            if "webSocketDebuggerUrl" not in target:
                pages = [
                    tab
                    for tab in _cdp_json(f"http://127.0.0.1:{port}/json/list")
                    if tab.get("type") == "page"
                ]
                target = next(
                    (tab for tab in pages if url_contains in tab.get("url", "")),
                    None,
                )
            if token is None or mcp_port is None:
                return True
            if target is None:
                return False
            return _connect_colab_tab(
                target["webSocketDebuggerUrl"], url, str(token), str(mcp_port)
            )
        _cdp_request(f"http://127.0.0.1:{port}/json/activate/{target['id']}")

        target_tab_url = target.get("url", "")
        current_base = target_tab_url.split("#")[0].split("?")[0]
        same_page = (current_base == clean_url) or (
            clean_url.endswith("empty.ipynb") and url_contains in current_base
        )
        if not same_page:
            _edge_cdp_call(
                target["webSocketDebuggerUrl"], "Page.navigate", {"url": url}
            )
            time.sleep(0.5)

        if token is not None and mcp_port is not None:
            return _connect_colab_tab(
                target["webSocketDebuggerUrl"], url, str(token), str(mcp_port)
            )
        return True
    except Exception as exc:
        logging.exception("Failed in _navigate_controlled_edge: %s", exc)
        return False


def _connect_colab_tab(
    websocket_url: str,
    url: str,
    token: str,
    mcp_port: str,
    *,
    retries: int = 1,
    retry_delay: float = 5.0,
) -> bool:
    for attempt in range(retries + 1):
        if attempt > 0:
            logging.info(
                "Colab tab connect attempt failed, waiting %.1fs before retrying (attempt %d/%d)...",
                retry_delay,
                attempt,
                retries,
            )
            time.sleep(retry_delay)
        if _perform_colab_tab_connection(websocket_url, url, token, mcp_port):
            return True
    return False


def _perform_colab_tab_connection(
    websocket_url: str, url: str, token: str, mcp_port: str, *, timeout: float = 20.0
) -> bool:
    deadline = time.monotonic() + timeout
    last_ready = None
    while time.monotonic() < deadline:
        try:
            ready = _edge_cdp_eval(
                websocket_url,
                "JSON.stringify((()=>{"
                "const notebook=window.colab?.global?.notebook;"
                "return {ready:document.readyState,"
                "hasService:!!notebook?.localColabMcpService,"
                "connected:!!notebook?.localColabMcpService?.isConnected?.(),"
                "token:sessionStorage.getItem('mcp_proxy_token'),"
                "port:sessionStorage.getItem('mcp_proxy_port')};"
                "})())",
            )
            last_ready = json.loads(ready.get("result", {}).get("value", "{}"))
            if last_ready.get("ready") == "complete" and last_ready.get("hasService"):
                break
        except Exception:
            pass
        time.sleep(1)
    if not last_ready or not last_ready.get("hasService"):
        return False

    target_token = json.dumps(token)
    target_port = json.dumps(mcp_port)
    target_url = json.dumps(url)
    expression = (
        "(async()=>{"
        "const svc=window.colab?.global?.notebook?.localColabMcpService;"
        "if(!svc) return JSON.stringify({ok:false,error:'localColabMcpService missing'});"
        f"const targetToken={target_token};"
        f"const targetPort={target_port};"
        f"const targetUrl={target_url};"
        "const before={connected:svc.isConnected?.(),token:sessionStorage.getItem('mcp_proxy_token'),port:sessionStorage.getItem('mcp_proxy_port'),href:location.href};"
        "const tokenChanged=Boolean(before.token&&before.token!==targetToken);"
        "const portChanged=Boolean(before.port&&String(before.port)!==String(targetPort));"
        "const mismatch=tokenChanged||portChanged;"
        "if(svc.isConnected?.()&&!mismatch){"
        "sessionStorage.setItem('mcp_proxy_token',targetToken);"
        "sessionStorage.setItem('mcp_proxy_port',targetPort);"
        "return JSON.stringify({ok:true,before,mismatch:false,alreadyConnected:true,connected:true,token:targetToken,port:targetPort,href:location.href});"
        "}"
        "if(mismatch&&svc.disconnect){try{await svc.disconnect();}catch(e){}}"
        "if(!svc.isConnected?.()&&svc.colabMcpServer?.closeServer){try{await svc.colabMcpServer.closeServer();}catch(e){}}"
        "sessionStorage.setItem('mcp_proxy_token',targetToken);"
        "sessionStorage.setItem('mcp_proxy_port',targetPort);"
        "history.replaceState(null,'',targetUrl);"
        "let alreadyConnected=false;"
        "try{await Promise.race([svc.connect(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('localColabMcpService.connect timed out')),20000))]);}"
        "catch(e){"
        "const msg=String(e?.message||e||'');"
        "if(msg.includes('MCP server already connected')&&svc.isConnected?.()){alreadyConnected=true;}"
        "else{throw e;}"
        "}"
        "return JSON.stringify({ok:true,before,mismatch,alreadyConnected,connected:svc.isConnected?.(),token:sessionStorage.getItem('mcp_proxy_token'),port:sessionStorage.getItem('mcp_proxy_port'),href:location.href});"
        "})()"
    )
    try:
        evaluation = _edge_cdp_eval(websocket_url, expression, await_promise=True)
    except Exception:
        return False
    if "exceptionDetails" in evaluation:
        return False
    value = evaluation.get("result", {}).get("value")
    if not isinstance(value, str):
        return False
    try:
        connected = json.loads(value)
    except json.JSONDecodeError:
        return False
    return bool(
        connected.get("ok")
        and connected.get("connected")
        and connected.get("token") == token
        and connected.get("port") == mcp_port
    )


def _edge_cdp_eval(websocket_url: str, expression: str, *, await_promise: bool = False):
    return _edge_cdp_call(
        websocket_url,
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        },
    )


def _edge_cdp_call(websocket_url: str, method: str, params: dict):
    ids = count(1)
    with websocket_connect(websocket_url, open_timeout=2) as ws:
        message_id = next(ids)
        ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message.get("result")


check_session_proxy_tool = Tool.from_function(
    fn=check_session_proxy_tool_fn,
    name=INJECTED_TOOL_NAME,
    description=(
        "Automatically connects to a Google Colab browser session via local browser automation and unlocks notebook editing tools. "
        "The connection is fully automated; NO user interaction or manual confirmation in the browser is needed. "
        "Optionally accepts a 'notebook_url' (e.g. https://colab.research.google.com/drive/xxx) "
        "to open/connect an existing notebook instead of an empty scratchpad. "
        "Returns a boolean representing whether the connection attempt succeeded."
    ),
)


class ColabSessionProxy:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.proxy_server: FastMCPProxy | None = None
        # list order matters, see: https://gofastmcp.com/servers/middleware#multiple-middleware
        self.middleware: list[Middleware] = []
        self.wss: ColabWebSocketServer | None = None

    async def start_proxy_server(self):
        self.wss = await self._exit_stack.enter_async_context(ColabWebSocketServer())
        proxy_client = await self._exit_stack.enter_async_context(
            ColabProxyClient(self.wss)
        )
        self.proxy_server = FastMCPProxy(
            client_factory=proxy_client.client_factory,
            instructions="Connects to a user's Google Colab session in a browser and allows for interactions with their Google Colab notebook",
        )
        # ColabProxyMiddleware must be first because it sets the fe_connected state
        self.middleware.append(ColabProxyMiddleware(proxy_client))
        self.middleware.append(
            ToolInjectionMiddleware(
                tools=[
                    check_session_proxy_tool,
                    *make_static_colab_proxy_tools(proxy_client),
                    *make_colab_management_tools(proxy_client),
                ]
            )
        )

    async def cleanup(self):
        await self._exit_stack.aclose()
