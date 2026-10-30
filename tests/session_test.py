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
import json
from colab_mcp import session
from fastmcp.server.middleware import MiddlewareContext
import pytest
from unittest.mock import patch, AsyncMock, Mock

ORIGINAL_NAVIGATE_CONTROLLED_EDGE = session._navigate_controlled_edge


@pytest.fixture(autouse=True)
def mock_browser_navigation(monkeypatch):
    mock_navigate = Mock(return_value=False)
    monkeypatch.setattr(session, "_navigate_controlled_edge", mock_navigate)
    return mock_navigate


@pytest.fixture
def mock_wss():
    """Provides a mock ColabWebSocketServer instance."""
    return MockColabWebSocketServer()


class MockColabWebSocketServer:
    def __init__(self):
        self.connection_live = asyncio.Event()
        self.read_stream = AsyncMock()
        self.write_stream = AsyncMock()
        self.token = "test-token"
        self.port = 1234

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def connection_info(self):
        return {
            "scratchUrl": "https://colab.research.google.com/notebooks/empty.ipynb",
            "token": self.token,
            "port": self.port,
        }


@pytest.fixture
def mock_proxy_client(mock_wss):
    client = Mock(spec=session.ColabProxyClient)
    client.wss = mock_wss
    client.is_connected.return_value = False
    return client


class TestColabProxyMiddleware:
    @pytest.mark.asyncio
    async def test_connection_live(self, mock_proxy_client):
        """Tests connection state change from disconnected to connected."""
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = True
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", True)
        context.fastmcp_context.set_state.assert_any_call("proxy_token", "test-token")
        context.fastmcp_context.set_state.assert_any_call("proxy_port", 1234)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_not_live(self, mock_proxy_client):
        """Tests connection state change from connected to disconnected."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = False
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", False)
        assert middleware.last_message_connected is False
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_connection_change(self, mock_proxy_client):
        """Tests no connection state change."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", True)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_call_tool_await_connection(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.side_effect = [False, True]
        mock_proxy_client.await_proxy_connection = AsyncMock()
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        context.fastmcp_context.report_progress.assert_called()
        assert result.structured_content == {"result": True}

    @pytest.mark.asyncio
    async def test_on_call_tool_timeout(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.return_value = False
        mock_proxy_client.await_proxy_connection = AsyncMock()
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        assert result.structured_content == {"result": False}


class TestCheckSessionProxyToolFn:
    @pytest.mark.asyncio
    async def test_connected(self):
        ctx = Mock()
        ctx.get_state.side_effect = (
            lambda k: True if k == session.FE_CONNECTED_KEY else None
        )
        assert await session.check_session_proxy_tool_fn(ctx) is True

    @pytest.mark.asyncio
    async def test_disconnected(self, mock_browser_navigation):
        ctx = Mock()

        def get_state(k):
            if k == session.FE_CONNECTED_KEY:
                return False
            if k == session.PROXY_TOKEN_KEY:
                return "test-token"
            if k == session.PROXY_PORT_KEY:
                return 1234
            return None

        ctx.get_state.side_effect = get_state
        assert await session.check_session_proxy_tool_fn(ctx) is False
        mock_browser_navigation.assert_called_once()
        args, _ = mock_browser_navigation.call_args
        assert "mcpProxyToken=test-token" in args[0]
        assert "mcpProxyPort=1234" in args[0]

    @pytest.mark.asyncio
    async def test_disconnected_with_custom_url(self, mock_browser_navigation):
        ctx = Mock()

        def get_state(k):
            if k == session.FE_CONNECTED_KEY:
                return False
            if k == session.PROXY_TOKEN_KEY:
                return "test-token"
            if k == session.PROXY_PORT_KEY:
                return 1234
            return None

        ctx.get_state.side_effect = get_state
        custom_url = "https://colab.research.google.com/drive/1ABCDEFG"
        assert (
            await session.check_session_proxy_tool_fn(ctx=ctx, notebook_url=custom_url)
            is False
        )
        mock_browser_navigation.assert_called_once()
        args, _ = mock_browser_navigation.call_args
        assert args[0] == f"{custom_url}#mcpProxyToken=test-token&mcpProxyPort=1234"

    @pytest.mark.asyncio
    async def test_disconnected_with_hash_url(self, mock_browser_navigation):
        ctx = Mock()

        def get_state(k):
            if k == session.FE_CONNECTED_KEY:
                return False
            if k == session.PROXY_TOKEN_KEY:
                return "test-token"
            if k == session.PROXY_PORT_KEY:
                return 1234
            return None

        ctx.get_state.side_effect = get_state
        url_with_hash = "https://colab.research.google.com/drive/1ABCDEFG#scrollTo=123"
        assert (
            await session.check_session_proxy_tool_fn(
                ctx=ctx, notebook_url=url_with_hash
            )
            is False
        )
        mock_browser_navigation.assert_called_once()
        args, _ = mock_browser_navigation.call_args
        assert (
            args[0]
            == "https://colab.research.google.com/drive/1ABCDEFG#mcpProxyToken=test-token&mcpProxyPort=1234"
        )

    @pytest.mark.asyncio
    async def test_connected_with_same_url_is_idempotent(
        self, mock_browser_navigation, monkeypatch
    ):
        ctx = Mock()
        ctx.get_state.side_effect = (
            lambda k: True if k == session.FE_CONNECTED_KEY else None
        )
        custom_url = "https://colab.research.google.com/drive/1ABCDEFG"
        session.set_active_notebook_url(custom_url)

        # 已经连接且请求相同的 notebook_url 时，应直接返回 True（幂等），不触发任何浏览器导航
        assert (
            await session.check_session_proxy_tool_fn(ctx=ctx, notebook_url=custom_url)
            is True
        )
        mock_browser_navigation.assert_not_called()


class TestConnectColabTabRetry:
    def test_connect_colab_tab_retry_after_5s(self, monkeypatch):
        mock_perform = Mock(side_effect=[False, True])
        mock_sleep = Mock()
        monkeypatch.setattr(session, "_perform_colab_tab_connection", mock_perform)
        monkeypatch.setattr(session.time, "sleep", mock_sleep)

        res = session._connect_colab_tab(
            "ws://debugger",
            "https://colab.research.google.com/drive/123",
            "test_token",
            "1234",
            retries=1,
            retry_delay=5.0,
        )

        assert res is True
        assert mock_perform.call_count == 2
        mock_sleep.assert_called_once_with(5.0)

    def test_connect_colab_tab_all_failures(self, monkeypatch):
        mock_perform = Mock(return_value=False)
        mock_sleep = Mock()
        monkeypatch.setattr(session, "_perform_colab_tab_connection", mock_perform)
        monkeypatch.setattr(session.time, "sleep", mock_sleep)

        res = session._connect_colab_tab(
            "ws://debugger",
            "https://colab.research.google.com/drive/123",
            "test_token",
            "1234",
            retries=1,
            retry_delay=5.0,
        )

        assert res is False
        assert mock_perform.call_count == 2
        mock_sleep.assert_called_once_with(5.0)


class TestNavigateControlledEdgeOptimization:
    def test_navigate_skips_reload_on_same_page(self, monkeypatch):
        monkeypatch.setattr(
            session, "_navigate_controlled_edge", ORIGINAL_NAVIGATE_CONTROLLED_EDGE
        )
        existing_tab = {
            "id": "tab1",
            "type": "page",
            "url": "https://colab.research.google.com/drive/1ABCDEFG#scrollTo=something",
            "webSocketDebuggerUrl": "ws://debugger/tab1",
        }
        monkeypatch.setattr(session, "_ensure_controlled_edge", Mock())
        monkeypatch.setattr(session, "_cdp_json", Mock(return_value=[existing_tab]))
        monkeypatch.setattr(session, "_cdp_request", Mock())
        mock_cdp_call = Mock()
        monkeypatch.setattr(session, "_edge_cdp_call", mock_cdp_call)
        mock_connect = Mock(return_value=True)
        monkeypatch.setattr(session, "_connect_colab_tab", mock_connect)

        url = "https://colab.research.google.com/drive/1ABCDEFG#mcpProxyToken=tok&mcpProxyPort=123"
        res = session._navigate_controlled_edge(url, token="tok", mcp_port="123")

        assert res is True
        mock_cdp_call.assert_not_called()
        mock_connect.assert_called_once()

    def test_navigate_performs_reload_on_different_page(self, monkeypatch):
        monkeypatch.setattr(
            session, "_navigate_controlled_edge", ORIGINAL_NAVIGATE_CONTROLLED_EDGE
        )
        existing_tab = {
            "id": "tab1",
            "type": "page",
            "url": "https://colab.research.google.com/drive/OLD_NOTEBOOK",
            "webSocketDebuggerUrl": "ws://debugger/tab1",
        }
        monkeypatch.setattr(session, "_ensure_controlled_edge", Mock())
        monkeypatch.setattr(session, "_cdp_json", Mock(return_value=[existing_tab]))
        monkeypatch.setattr(session, "_cdp_request", Mock())
        mock_cdp_call = Mock()
        monkeypatch.setattr(session, "_edge_cdp_call", mock_cdp_call)
        mock_connect = Mock(return_value=True)
        monkeypatch.setattr(session, "_connect_colab_tab", mock_connect)
        monkeypatch.setattr(session.time, "sleep", Mock())

        url = "https://colab.research.google.com/drive/NEW_NOTEBOOK#mcpProxyToken=tok&mcpProxyPort=123"
        res = session._navigate_controlled_edge(url, token="tok", mcp_port="123")

        assert res is True
        mock_cdp_call.assert_called_once_with(
            "ws://debugger/tab1", "Page.navigate", {"url": url}
        )
        mock_connect.assert_called_once()


def test_resolve_notebook_url():
    session._ACTIVE_NOTEBOOK_URL = None
    assert (
        session.resolve_notebook_url(None) == f"{session.COLAB}{session.SCRATCH_PATH}"
    )
    assert session.resolve_notebook_url("") == f"{session.COLAB}{session.SCRATCH_PATH}"
    assert (
        session.resolve_notebook_url("https://colab.research.google.com/drive/123#hash")
        == "https://colab.research.google.com/drive/123"
    )
    assert session.resolve_notebook_url("/drive/123") == f"{session.COLAB}/drive/123"


class TestControlledEdgeLaunch:
    def test_ensure_controlled_edge_reuses_live_cdp(self, monkeypatch):
        popen = Mock()
        monkeypatch.setattr(session, "_cdp_alive", Mock(return_value=True))
        monkeypatch.setattr(session.subprocess, "Popen", popen)

        session._ensure_controlled_edge("https://example.test", port="9333")

        popen.assert_not_called()

    def test_ensure_controlled_edge_starts_edge(self, monkeypatch, tmp_path):
        alive_results = iter([False, False, True])
        popen = Mock()
        edge_path = tmp_path / "msedge.exe"
        edge_path.write_text("", encoding="utf-8")
        profile = tmp_path / "profile"
        monkeypatch.setenv(session.EDGE_PROFILE_ENV, str(profile))
        monkeypatch.setattr(
            session, "_cdp_alive", Mock(side_effect=lambda _port: next(alive_results))
        )
        monkeypatch.setattr(
            session, "_edge_executable_path", Mock(return_value=str(edge_path))
        )
        monkeypatch.setattr(session.subprocess, "Popen", popen)
        monkeypatch.setattr(session.time, "sleep", Mock())

        session._ensure_controlled_edge("https://example.test", port="9333")

        assert profile.exists()
        popen.assert_called_once()
        command = popen.call_args.args[0]
        assert command[0] == str(edge_path)
        assert "--remote-debugging-port=9333" in command
        assert f"--user-data-dir={profile}" in command
        assert command[-1] == "https://example.test"


class TestColabProxyClient:
    def test_is_connected(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.is_connected() is False
        mock_wss.connection_live.set()
        assert client.is_connected() is False
        client.proxy_mcp_client = Mock()
        assert client.is_connected() is True

    def test_client_factory_connection_live(self, mock_wss):
        mock_wss.connection_live.set()
        client = session.ColabProxyClient(mock_wss)
        client.proxy_mcp_client = Mock()

        assert client.client_factory() is client.proxy_mcp_client

    def test_client_factory_connection_not_live(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.client_factory() is client.stubbed_mcp_client

    @pytest.mark.asyncio
    async def test_await_proxy_connection(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        client._start_task = asyncio.create_task(asyncio.sleep(0.01))
        mock_wss.connection_live.set()
        with patch("colab_mcp.session.UI_CONNECTION_TIMEOUT", 0.1):
            await client.await_proxy_connection()
        await client._start_task

    @pytest.mark.asyncio
    @patch("colab_mcp.session.Client")
    @patch("colab_mcp.session.ColabTransport", spec=session.ColabTransport)
    async def test_start_proxy_client(
        self, mock_colab_transport, mock_client, mock_wss
    ):
        mock_client.return_value.__aenter__ = AsyncMock()
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        async with client:
            await client._start_task

        mock_colab_transport.assert_called_once_with(mock_wss)
        mock_client.assert_called_with(mock_colab_transport.return_value)

    @pytest.mark.asyncio
    @patch("colab_mcp.session.Client")
    @patch("colab_mcp.session.ColabTransport", spec=session.ColabTransport)
    async def test_reconnection_lifecycle(
        self, mock_colab_transport, mock_client, mock_wss
    ):
        mock_client.return_value.__aenter__ = AsyncMock()
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        mock_wss.connection_generation = 1
        async with client:
            await client._start_task
            # Initial calls: 1 for stubbed client in __init__, 1 for proxy client in _start_proxy_client
            assert mock_client.call_count == 2

            # Simulate disconnect
            mock_wss.connection_live.clear()
            await asyncio.sleep(0.3)
            assert client.is_connected() is False

            # Simulate reconnect
            mock_wss.connection_generation = 2
            mock_wss.connection_live.set()
            await asyncio.sleep(0.4)
            # Reconnect creates a fresh Client instance
            assert mock_client.call_count == 3


class TestColabTransport:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ClientSession")
    async def test_connect_session(self, mock_client_session, mock_wss):
        transport = session.ColabTransport(mock_wss)
        mock_client_session.return_value.__aenter__ = AsyncMock()
        async with transport.connect_session(foo="bar") as client_session:
            assert (
                client_session
                == mock_client_session.return_value.__aenter__.return_value
            )

        mock_client_session.assert_called_once_with(
            mock_wss.read_stream, mock_wss.write_stream, foo="bar"
        )


class TestColabSessionProxy:
    def test_static_tool_names(self, mock_proxy_client):
        static_tools = session.make_static_colab_proxy_tools(mock_proxy_client)
        management_tools = session.make_colab_management_tools(mock_proxy_client)
        names = {tool.name for tool in [*static_tools, *management_tools]}

        assert {
            "add_code_cell",
            "add_text_cell",
            "delete_cell",
            "get_cells",
            "move_cell",
            "run_code_cell",
            "update_cell",
            "set_env_vars",
            "restart_runtime",
            "shutdown_runtime",
            "rerun_env_setup_cells",
            "check_runtime",
            "connect_runtime",
            "check_gpu",
            "set_runtime_accelerator",
            "import_notebook",
            "export_notebook",
            "download_notebook",
            "upload_notebook",
            "run_code_cells",
            "run_cell_range",
            "run_all_cells",
            "cancel_queued_cells",
            "get_cell",
            "find_cells",
            "replace_cells",
            "patch_cell",
            "get_cell_status",
            "wait_for_cells",
            "read_cell_outputs",
            "watch_cell_outputs",
            "get_env_vars",
            "unset_env_vars",
            "load_env_file",
            "list_background_commands",
            "watch_background_command",
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
            "sample_gpu_usage",
            "start_gpu_monitor",
            "stop_gpu_monitor",
            "read_gpu_monitor",
        }.issubset(names)
        assert "start_here" not in names
        assert "get_ai_tool_guide" not in names
        assert "get_runtime_accelerator_state" not in names

    def test_shutdown_runtime_tool_description_guides_cleanup(self, mock_proxy_client):
        management_tools = session.make_colab_management_tools(mock_proxy_client)
        shutdown_tool = next(
            tool for tool in management_tools if tool.name == "shutdown_runtime"
        )

        assert "releases/unassigns" in shutdown_tool.description
        assert "final cleanup step" in shutdown_tool.description
        assert (
            "Download needed weights, logs, and artifacts first"
            in shutdown_tool.description
        )
        assert "does not uninstall the MCP server" in shutdown_tool.description
        assert "does not close the browser tab" in shutdown_tool.description

    @pytest.mark.asyncio
    @patch("colab_mcp.session.ToolInjectionMiddleware")
    @patch("colab_mcp.session.ColabWebSocketServer")
    @patch("colab_mcp.session.ColabProxyClient")
    @patch("colab_mcp.session.ColabProxyMiddleware")
    async def test_start_proxy_server(
        self,
        mock_colab_proxy_middleware,
        mock_colab_proxy_client,
        mock_colab_web_socket_server,
        mock_tool_injection_middleware,
    ):
        mock_colab_web_socket_server.return_value.__aenter__ = AsyncMock()
        mock_colab_proxy_client.return_value.__aenter__ = AsyncMock()
        proxy = session.ColabSessionProxy()
        await proxy.start_proxy_server()
        mock_colab_proxy_client.assert_called_once()
        assert proxy.proxy_server is not None
        mock_colab_proxy_middleware.assert_called_once()
        mock_tool_injection_middleware.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup(self):
        proxy = session.ColabSessionProxy()
        proxy._exit_stack = AsyncMock()
        await proxy.cleanup()
        proxy._exit_stack.aclose.assert_called_once()


class TestOutputNormalization:
    def test_structured_defaults(self):
        structured = session.ColabRuntimeManagementTool._structured({"value": 1})

        assert structured == {
            "value": 1,
            "ok": True,
            "status": "ok",
            "warnings": [],
        }

    def test_structured_marks_warnings(self):
        structured = session.ColabRuntimeManagementTool._structured(
            {"value": 1, "warnings": ["needs reconnect"]}
        )

        assert structured["ok"] is False
        assert structured["status"] == "warning"

    def test_structured_marks_error(self):
        structured = session.ColabRuntimeManagementTool._structured(
            {"value": 1, "ok": False, "error": "runtime disconnected"}
        )

        assert structured["ok"] is False
        assert structured["status"] == "error"
        assert structured["warnings"] == ["runtime disconnected"]

    def test_normalize_stream_error_and_display_outputs(self):
        outputs = [
            {"output_type": "stream", "name": "stdout", "text": ["hello\n"]},
            {
                "output_type": "display_data",
                "data": {"text/plain": ["<Figure size>"]},
                "metadata": {"foo": "bar"},
            },
            {
                "output_type": "error",
                "ename": "ValueError",
                "evalue": "bad",
                "traceback": ["line 1", "ValueError: bad"],
            },
        ]

        chunks = session.ColabRuntimeManagementTool._normalize_outputs(
            outputs, observed_at=123.0
        )

        assert chunks[0]["outputType"] == "stream"
        assert chunks[0]["text"] == "hello\n"
        assert chunks[1]["text"] == "<Figure size>"
        assert chunks[1]["metadata"] == {"foo": "bar"}
        assert chunks[2]["outputType"] == "error"
        assert chunks[2]["ename"] == "ValueError"
        assert "ValueError: bad" in chunks[2]["text"]
        assert all(chunk["timestamp"] == 123.0 for chunk in chunks)

    def test_notebook_document_preserves_cells_outputs_metadata_and_unicode(self):
        cells = [
            {
                "cell_type": "markdown",
                "id": "md-1",
                "metadata": {"custom": "值"},
                "source": ["# 标题\n"],
            },
            {
                "cell_type": "code",
                "id": "code-1",
                "execution_count": 7,
                "metadata": {"tags": ["keep"]},
                "source": ["print('你好')\n"],
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": ["你好\n"]}
                ],
            },
        ]

        notebook = session.ColabRuntimeManagementTool._notebook_document(
            cells, name="测试.ipynb"
        )

        assert notebook["nbformat"] == 4
        assert notebook["metadata"]["colab"]["name"] == "测试.ipynb"
        assert notebook["metadata"]["kernelspec"]["name"] == "python3"
        assert notebook["cells"][0]["metadata"] == {"custom": "值"}
        assert notebook["cells"][1]["execution_count"] == 7
        assert notebook["cells"][1]["outputs"][0]["text"] == ["你好\n"]

    def test_cell_status_idle_success_and_error(self):
        idle = session.ColabRuntimeManagementTool._cell_status(
            {"id": "a", "cell_type": "code", "source": ["x=1"]}, 0
        )
        success = session.ColabRuntimeManagementTool._cell_status(
            {
                "id": "b",
                "cell_type": "code",
                "execution_count": 1,
                "outputs": [{"output_type": "stream", "text": ["ok\n"]}],
            },
            1,
        )
        error = session.ColabRuntimeManagementTool._cell_status(
            {
                "id": "c",
                "cell_type": "code",
                "execution_count": 2,
                "outputs": [
                    {
                        "output_type": "error",
                        "ename": "ValueError",
                        "evalue": "bad",
                    }
                ],
            },
            2,
        )

        assert idle["state"] == "idle"
        assert success["state"] == "success"
        assert success["lastOutputText"] == "ok\n"
        assert error["state"] == "error"
        assert error["hasError"] is True

    def test_redacted_env(self):
        values = {
            "NORMAL": "visible",
            "API_KEY": "secret",
            "ACCESS_TOKEN": "token",
            "PASSWORD": "pw",
        }

        redacted = session.ColabRuntimeManagementTool._redacted_env(
            values, ["KEY", "TOKEN", "PASSWORD"]
        )

        assert redacted["NORMAL"] == "visible"
        assert redacted["API_KEY"] == "<redacted>"
        assert redacted["ACCESS_TOKEN"] == "<redacted>"
        assert redacted["PASSWORD"] == "<redacted>"

    def test_parse_nvidia_smi_gpu_csv(self):
        stdout = "0, Tesla T4, 15360, 1024, 14336, 55, 12, 70, 35.5\n"

        gpus = session.ColabRuntimeManagementTool._parse_nvidia_smi_gpu_csv(stdout)

        assert gpus == [
            {
                "gpuIndex": "0",
                "name": "Tesla T4",
                "memoryTotalMiB": "15360",
                "memoryUsedMiB": "1024",
                "memoryFreeMiB": "14336",
                "gpuUtilizationPercent": "55",
                "memoryUtilizationPercent": "12",
                "temperatureC": "70",
                "powerDrawW": "35.5",
            }
        ]


def tool_result(payload):
    return Mock(content=[session.TextContent(type="text", text=json.dumps(payload))])


def management_tool(mock_proxy_client, name):
    return session.ColabRuntimeManagementTool(
        proxy_client=mock_proxy_client,
        name=name,
        description="test",
        parameters={},
    )


class TestManagementToolLogic:
    @pytest.mark.asyncio
    async def test_run_code_cells_stop_on_error(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "run_code_cells")
        tool._call_colab = AsyncMock(
            side_effect=[
                tool_result({"outputs": [{"output_type": "stream", "text": ["ok\n"]}]}),
                tool_result(
                    {
                        "outputs": [
                            {
                                "output_type": "error",
                                "ename": "ValueError",
                                "evalue": "bad",
                            }
                        ]
                    }
                ),
            ]
        )

        result = await tool.run(
            {"cellIds": ["a", "b", "c"], "stopOnError": True, "includeOutputs": True}
        )

        assert result.structured_content["cellCount"] == 2
        assert result.structured_content["cells"][0]["status"] == "success"
        assert result.structured_content["cells"][1]["status"] == "error"
        assert tool._call_colab.await_count == 2

    @pytest.mark.asyncio
    async def test_get_cell_status_reports_missing_ids(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "get_cell_status")
        tool._call_colab = AsyncMock(
            return_value=tool_result(
                {"cells": [{"id": "a", "cell_type": "code", "source": ["x=1"]}]}
            )
        )

        with pytest.raises(ValueError, match="Cell\\(s\\) not found"):
            await tool.run({"cellIds": ["missing"]})

    @pytest.mark.asyncio
    async def test_replace_cells_inserts_markdown_and_code(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "replace_cells")
        tool._call_colab = AsyncMock(
            side_effect=[
                tool_result({"cells": []}),
                tool_result({"newCellId": "md"}),
                tool_result({"newCellId": "code"}),
            ]
        )

        result = await tool.run(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# hi"]},
                    {"cell_type": "code", "source": ["print(1)"]},
                ]
            }
        )

        assert result.structured_content["inserted"] == [
            {"cellId": "md", "cellType": "markdown"},
            {"cellId": "code", "cellType": "code"},
        ]
        assert tool._call_colab.await_args_list[1].args[0] == "add_text_cell"
        assert tool._call_colab.await_args_list[2].args[0] == "add_code_cell"

    @pytest.mark.asyncio
    async def test_patch_cell_requires_source_or_content(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "patch_cell")

        with pytest.raises(ValueError, match="source or content"):
            await tool.run({"cellId": "a"})

    @pytest.mark.asyncio
    async def test_patch_cell_target_replacement(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "patch_cell")
        tool._call_colab = AsyncMock(
            side_effect=[
                tool_result({"cells": [{"id": "c1", "source": "x = 10\ny = 20\n"}]}),
                tool_result({"cellId": "c1", "updated": True}),
            ]
        )

        result = await tool.run(
            {
                "cellId": "c1",
                "target": "x = 10",
                "replacement": "x = 99",
            }
        )

        assert tool._call_colab.await_count == 2
        assert tool._call_colab.await_args_list[0].args == (
            "get_cells",
            {"includeOutputs": False},
        )
        assert tool._call_colab.await_args_list[1].args == (
            "update_cell",
            {"cellId": "c1", "content": "x = 99\ny = 20\n"},
        )
        assert result.structured_content["patched"] is True
        assert result.structured_content["replacedCount"] == 1

    @pytest.mark.asyncio
    async def test_patch_cell_target_with_count(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "patch_cell")
        tool._call_colab = AsyncMock(
            side_effect=[
                tool_result({"cells": [{"id": "c1", "source": "foo foo foo"}]}),
                tool_result({"cellId": "c1", "updated": True}),
            ]
        )

        result = await tool.run(
            {
                "cellId": "c1",
                "target": "foo",
                "replacement": "bar",
                "count": 1,
            }
        )

        assert tool._call_colab.await_args_list[1].args == (
            "update_cell",
            {"cellId": "c1", "content": "bar foo foo"},
        )
        assert result.structured_content["replacedCount"] == 1
        assert result.structured_content["occurrences"] == 3

    @pytest.mark.asyncio
    async def test_patch_cell_target_not_found(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "patch_cell")
        tool._call_colab = AsyncMock(
            return_value=tool_result({"cells": [{"id": "c1", "source": "x = 1"}]})
        )

        with pytest.raises(ValueError, match="Target text not found"):
            await tool.run({"cellId": "c1", "target": "y = 2"})

    @pytest.mark.asyncio
    async def test_patch_cell_cell_not_found(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "patch_cell")
        tool._call_colab = AsyncMock(
            return_value=tool_result({"cells": [{"id": "c1", "source": "x = 1"}]})
        )

        with pytest.raises(ValueError, match="Cell not found"):
            await tool.run({"cellId": "c2", "target": "x = 1"})

    @pytest.mark.asyncio
    async def test_shell_tools_generate_runtime_scripts(self, mock_proxy_client):
        run_shell = management_tool(mock_proxy_client, "run_shell_command")
        run_shell._run_terminal_python = AsyncMock(return_value={})
        await run_shell.run(
            {
                "command": "pwd && echo out && echo err >&2",
                "timeoutSeconds": 7,
                "cwd": "/content/work",
            }
        )
        code = run_shell._run_terminal_python.await_args.args[0]
        assert "timeout=timeout_seconds" in code
        assert "capture_output=True" in code
        assert "cwd=cwd or None" in code
        assert "'stdout': proc.stdout" in code
        assert "'stderr': proc.stderr" in code
        assert "subprocess.TimeoutExpired" in code
        assert "COLAB_MCP_RESULT_MARKER" in code

        start = management_tool(mock_proxy_client, "start_background_command")
        start._run_terminal_python = AsyncMock(return_value={})
        await start.run(
            {"command": "python train.py", "name": "train", "cwd": "/content/work"}
        )
        code = start._run_terminal_python.await_args.args[0]
        assert "subprocess.Popen" in code
        assert "registry[name]" in code
        assert "status_path" in code
        assert "returncode" in code
        assert "finishedAt" in code
        assert "'cwd': cwd" in code
        assert "stderr=subprocess.STDOUT" in code

        check = management_tool(mock_proxy_client, "check_background_command")
        check._run_terminal_python = AsyncMock(return_value={})
        await check.run({"name": "missing"})
        code = check._run_terminal_python.await_args.args[0]
        assert "entry = registry[name]" in code
        assert "os.kill(pid, 0)" in code
        assert "entry.update(json.load(f))" in code

        tail = management_tool(mock_proxy_client, "tail_file")
        tail._run_terminal_python = AsyncMock(return_value={})
        await tail.run({"path": "/content/log.txt", "lines": 12, "maxBytes": 1234})
        code = tail._run_terminal_python.await_args.args[0]
        assert "f.seek(max(0, size - max_bytes))" in code
        assert "splitlines()[-lines:]" in code

        stop = management_tool(mock_proxy_client, "stop_background_command")
        stop._run_terminal_python = AsyncMock(return_value={})
        await stop.run({"name": "train", "signal": 9})
        code = stop._run_terminal_python.await_args.args[0]
        assert "os.killpg(pid, sig)" in code
        assert "'signal': sig" in code

        watch = management_tool(mock_proxy_client, "watch_background_command")
        watch._run_terminal_python = AsyncMock(return_value={})
        await watch.run({"name": "train", "lines": 5})
        code = watch._run_terminal_python.await_args.args[0]
        assert "commands" in code
        assert "item['tail']" in code

    @pytest.mark.asyncio
    async def test_file_tools_use_runtime_contents_api(self, mock_proxy_client):
        upload = management_tool(mock_proxy_client, "upload_file")
        with patch(
            "colab_mcp.session._runtime_upload_file",
            return_value={"path": "/content/数据/hello.txt"},
        ):
            result = await upload.run(
                {
                    "path": "/content/数据/hello.txt",
                    "contentBase64": "5L2g5aW9",
                    "overwrite": False,
                    "makeParents": True,
                }
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        upload_local = management_tool(mock_proxy_client, "upload_local_file")
        with patch(
            "colab_mcp.session._runtime_upload_local_file",
            return_value={"localPath": "C:/tmp/in.txt", "path": "/content/in.txt"},
        ):
            result = await upload_local.run(
                {
                    "localPath": "C:/tmp/in.txt",
                    "path": "/content/in.txt",
                    "overwrite": True,
                    "makeParents": True,
                }
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        download = management_tool(mock_proxy_client, "download_file")
        with patch(
            "colab_mcp.session._runtime_download_file",
            return_value={"contentBase64": "5L2g5aW9"},
        ):
            result = await download.run(
                {"path": "/content/数据/hello.txt", "offset": 2, "maxBytes": 8}
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        download_local = management_tool(mock_proxy_client, "download_file_to_local")
        with patch(
            "colab_mcp.session._runtime_download_file_to_local",
            return_value={"localPath": "C:/tmp/out.txt", "path": "/content/out.txt"},
        ):
            result = await download_local.run(
                {
                    "path": "/content/out.txt",
                    "localPath": "C:/tmp/out.txt",
                    "overwrite": True,
                    "makeParents": True,
                }
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        chunk = management_tool(mock_proxy_client, "upload_file_chunk")
        with patch(
            "colab_mcp.session._runtime_upload_file_chunk",
            return_value={"chunkBytes": 3},
        ):
            result = await chunk.run(
                {
                    "uploadId": "run/1",
                    "path": "/content/out.bin",
                    "chunkBase64": "AAEC",
                    "chunkIndex": 0,
                    "overwrite": False,
                }
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        complete = management_tool(mock_proxy_client, "complete_upload")
        with patch(
            "colab_mcp.session._runtime_complete_upload", return_value={"size": 3}
        ):
            result = await complete.run(
                {"uploadId": "run/1", "path": "/content/out.bin", "overwrite": True}
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        download_chunk = management_tool(mock_proxy_client, "download_file_chunk")
        with patch(
            "colab_mcp.session._runtime_download_file_chunk",
            return_value={"done": False},
        ):
            result = await download_chunk.run(
                {"path": "/content/out.bin", "offset": 4, "maxBytes": 16}
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        stat_file = management_tool(mock_proxy_client, "stat_file")
        with patch(
            "colab_mcp.session._runtime_stat_file", return_value={"exists": True}
        ):
            result = await stat_file.run({"path": "/content/out.bin"})
        assert result.structured_content["executionBackend"] == "colab-file-api"

        list_files = management_tool(mock_proxy_client, "list_files")
        with patch(
            "colab_mcp.session._runtime_list_files", return_value={"entries": []}
        ):
            result = await list_files.run(
                {"path": "/content/数据", "recursive": True, "maxEntries": 2}
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        delete = management_tool(mock_proxy_client, "delete_file")
        with patch(
            "colab_mcp.session._runtime_delete_file", return_value={"deleted": True}
        ):
            result = await delete.run({"path": "/content/out.bin", "recursive": False})
        assert result.structured_content["executionBackend"] == "colab-file-api"

        mkdir = management_tool(mock_proxy_client, "make_directory")
        with patch(
            "colab_mcp.session._runtime_make_directory", return_value={"created": True}
        ):
            result = await mkdir.run(
                {"path": "/content/数据/new", "parents": True, "existOk": True}
            )
        assert result.structured_content["executionBackend"] == "colab-file-api"

        upload._run_terminal_python = AsyncMock(return_value={})
        with patch(
            "colab_mcp.session._runtime_upload_file",
            return_value={"path": "/content/out.bin"},
        ):
            await upload.run(
                {"path": "/content/out.bin", "contentBase64": "AA==", "overwrite": True}
            )
        upload._run_terminal_python.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_check_runtime_includes_accelerator_ui_state(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "check_runtime")
        tool._run_terminal_python = AsyncMock(
            return_value={
                "executionBackend": "colab-terminal",
                "python": "3.13",
            }
        )
        with (
            patch("colab_mcp.session._navigate_controlled_edge") as mock_nav,
            patch(
                "colab_mcp.session._evaluate_colab_page",
                return_value={
                    "result": {
                        "value": '{"ok": true, "hasGpuHint": true, "options": []}'
                    }
                },
            ),
        ):
            result = await tool.run({})

        mock_nav.assert_not_called()
        assert result.structured_content["python"] == "3.13"
        assert result.structured_content["acceleratorUiState"]["hasGpuHint"] is True

    @pytest.mark.asyncio
    async def test_check_runtime_warns_when_accelerator_ui_state_unavailable(
        self, mock_proxy_client
    ):
        tool = management_tool(mock_proxy_client, "check_runtime")
        tool._run_terminal_python = AsyncMock(
            return_value={
                "executionBackend": "colab-terminal",
                "python": "3.13",
            }
        )
        with (
            patch(
                "colab_mcp.session._evaluate_colab_page",
                side_effect=RuntimeError("browser unavailable"),
            ),
            patch("colab_mcp.session._navigate_controlled_edge") as mock_nav,
        ):
            result = await tool.run({})

        mock_nav.assert_not_called()
        assert result.structured_content["status"] == "warning"
        assert (
            "Could not inspect accelerator UI state"
            in result.structured_content["warnings"][0]
        )

    def test_terminal_tool_result_marks_warnings(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "check_gpu")
        result = tool._terminal_tool_result(
            {
                "executionBackend": "colab-terminal",
                "warnings": ["No GPU is active."],
            }
        )

        assert result.structured_content["ok"] is False
        assert result.structured_content["status"] == "warning"

    def test_runtime_tool_result_marks_warnings(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "stat_file")
        result = tool._runtime_tool_result(
            {
                "executionBackend": "colab-file-api",
                "warnings": ["File is truncated."],
            }
        )

        assert result.structured_content["ok"] is False
        assert result.structured_content["status"] == "warning"

    def test_tool_result_marks_error_without_warnings(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "check_gpu")
        result = tool._terminal_tool_result(
            {
                "executionBackend": "colab-terminal",
                "ok": False,
                "error": "runtime disconnected",
            }
        )

        assert result.structured_content["ok"] is False
        assert result.structured_content["status"] == "error"
        assert result.structured_content["warnings"] == ["runtime disconnected"]

    def test_terminal_tool_result_marks_nonzero_returncode_error(
        self, mock_proxy_client
    ):
        tool = management_tool(mock_proxy_client, "run_shell_command")
        result = tool._terminal_tool_result(
            {
                "executionBackend": "colab-terminal",
                "returncode": 2,
                "stderr": "bad command",
            }
        )

        assert result.structured_content["ok"] is False
        assert result.structured_content["status"] == "error"

    def test_terminal_tool_result_marks_timeout_error(self, mock_proxy_client):
        tool = management_tool(mock_proxy_client, "run_shell_command")
        result = tool._terminal_tool_result(
            {
                "executionBackend": "colab-terminal",
                "timed_out": True,
                "timeout_seconds": 1,
            }
        )

        assert result.structured_content["ok"] is False
        assert result.structured_content["status"] == "error"


class TestLocalFileTransfer:
    def test_upload_local_file_accepts_resolved_local_path(self, tmp_path):
        source = tmp_path / "input.txt"
        source.write_text("hello", encoding="utf-8")

        with patch(
            "colab_mcp.session._runtime_upload_file",
            return_value={"path": "/content/input.txt"},
        ) as upload:
            result = session._runtime_upload_local_file(
                str(source),
                "/content/input.txt",
                overwrite=True,
            )

        assert result["localPath"] == str(source.resolve())
        upload.assert_called_once()

    def test_download_file_to_local_accepts_resolved_local_path(self, tmp_path):
        target = tmp_path / "nested" / "out.txt"

        with patch(
            "colab_mcp.session._runtime_download_bytes",
            return_value=(b"hello", 5),
        ):
            result = session._runtime_download_file_to_local(
                "/content/out.txt",
                str(target),
                overwrite=True,
            )

        assert target.read_bytes() == b"hello"
        assert result["localPath"] == str(target.resolve())

    def test_local_file_path_requires_value(self):
        with pytest.raises(ValueError, match="Local path is required"):
            session._runtime_upload_local_file("", "/content/out.txt")

    @pytest.mark.asyncio
    async def test_connect_runtime_does_not_navigate_when_connected(
        self, mock_proxy_client
    ):
        tool = management_tool(mock_proxy_client, "connect_runtime")
        mock_proxy_client.is_connected.return_value = True
        with (
            patch("colab_mcp.session._navigate_controlled_edge") as mock_nav,
            patch(
                "colab_mcp.session._evaluate_colab_page",
                return_value={"result": {"value": '{"ok": true}'}},
            ),
        ):
            result = await tool.run({})

        mock_nav.assert_not_called()
        assert result.structured_content["ok"] is True

    def test_colab_page_websocket_url_prefers_drive_tab(self, monkeypatch):
        tabs = [
            {
                "type": "page",
                "url": "https://colab.research.google.com/notebooks/empty.ipynb",
                "webSocketDebuggerUrl": "ws://empty",
            },
            {
                "type": "page",
                "url": "https://colab.research.google.com/drive/1QYf0txvcL0EA",
                "webSocketDebuggerUrl": "ws://drive",
            },
        ]
        monkeypatch.setattr(session, "_cdp_json", Mock(return_value=tabs))
        session._ACTIVE_NOTEBOOK_URL = None
        ws_url = session._colab_page_websocket_url()
        assert ws_url == "ws://drive"

    def test_colab_page_websocket_url_prefers_active_notebook(self, monkeypatch):
        tabs = [
            {
                "type": "page",
                "url": "https://colab.research.google.com/drive/OTHER",
                "webSocketDebuggerUrl": "ws://other",
            },
            {
                "type": "page",
                "url": "https://colab.research.google.com/drive/TARGET_123",
                "webSocketDebuggerUrl": "ws://target",
            },
        ]
        monkeypatch.setattr(session, "_cdp_json", Mock(return_value=tabs))
        session.set_active_notebook_url(
            "https://colab.research.google.com/drive/TARGET_123"
        )
        try:
            ws_url = session._colab_page_websocket_url()
            assert ws_url == "ws://target"
        finally:
            session._ACTIVE_NOTEBOOK_URL = None

    @pytest.mark.asyncio
    async def test_read_cell_outputs_defaults_to_latest_20_lines(
        self, mock_proxy_client
    ):
        tool = management_tool(mock_proxy_client, "read_cell_outputs")
        fifty_lines = "\n".join(f"line {i}" for i in range(50)) + "\n"
        tool._call_colab = AsyncMock(
            return_value=tool_result(
                {
                    "cells": [
                        {
                            "id": "c1",
                            "outputs": [{"output_type": "stream", "text": fifty_lines}],
                        }
                    ]
                }
            )
        )

        # 1. 默认调用：只保留最新 20 行，带真实行号且默认不返回冗余的 outputs 与 chunks
        result = await tool.run({"cellIds": ["c1"]})
        assert result.structured_content["totalLines"]["c1"] == 50
        assert result.structured_content["returnedLines"]["c1"] == 20
        tail_text = result.structured_content["tailText"]["c1"]
        assert "50: line 49" in tail_text
        assert "31: line 30" in tail_text
        assert "line 29" not in tail_text
        assert len(tail_text.splitlines()) == 20
        assert "outputs" not in result.structured_content
        assert "chunks" not in result.structured_content
        assert '"outputs":' not in result.content[0].text
        assert '"chunks":' not in result.content[0].text

        # 2. 自定义 lines=5，行号从 46 到 50
        result_5 = await tool.run({"cellIds": ["c1"], "lines": 5})
        assert result_5.structured_content["returnedLines"]["c1"] == 5
        tail_5 = result_5.structured_content["tailText"]["c1"]
        assert "50: line 49" in tail_5
        assert "46: line 45" in tail_5
        assert "line 44" not in tail_5
        assert len(tail_5.splitlines()) == 5

        # 3. 指定 lines=0 返回全部行，行号从 1 到 50
        result_all = await tool.run({"cellIds": ["c1"], "lines": 0})
        assert result_all.structured_content["returnedLines"]["c1"] == 50
        assert "1: line 0" in result_all.structured_content["tailText"]["c1"]
        assert "50: line 49" in result_all.structured_content["tailText"]["c1"]

        # 4. 显式开启 includeRaw 与 normalize
        result_raw = await tool.run(
            {"cellIds": ["c1"], "includeRaw": True, "normalize": True}
        )
        assert "outputs" in result_raw.structured_content
        assert "chunks" in result_raw.structured_content
        assert '"outputs":' in result_raw.content[0].text
        assert '"chunks":' in result_raw.content[0].text

    @pytest.mark.asyncio
    async def test_read_cell_outputs_strips_ansi_and_truncates_long_lines(
        self, mock_proxy_client
    ):
        tool = management_tool(mock_proxy_client, "read_cell_outputs")
        ansi_text = "\x1b[35mTraceback\x1b[0m: \x1b[31mError\x1b[0m\n"
        huge_line = "A" * 5000 + "\n"
        tool._call_colab = AsyncMock(
            return_value=tool_result(
                {
                    "cells": [
                        {
                            "id": "c1",
                            "outputs": [
                                {
                                    "output_type": "stream",
                                    "text": ansi_text + huge_line,
                                }
                            ],
                        }
                    ]
                }
            )
        )

        result = await tool.run({"cellIds": ["c1"], "maxLineChars": 200})
        tail_text = result.structured_content["tailText"]["c1"]
        # ANSI 控制符应被完全剥离
        assert "\x1b[" not in tail_text
        assert "Traceback: Error" in tail_text
        # 超长行应被优雅折叠截断
        assert "A" * 250 not in tail_text
        assert "[truncated" in tail_text
        assert "chars]" in tail_text

class TestStaticColabProxyTool:
    @pytest.mark.asyncio
    async def test_get_cells_defaults_to_no_outputs(self, mock_proxy_client):
        mock_proxy_client.is_connected.return_value = True
        mock_proxy_client.proxy_mcp_client = AsyncMock()
        raw_cells_response = {
            "cells": [
                {
                    "id": "c1",
                    "cell_type": "code",
                    "source": ["x = 1\n", "print(x)\n"],
                    "outputs": [
                        {"output_type": "stream", "text": "1\n"}
                    ],
                },
                {
                    "id": "c2",
                    "cell_type": "code",
                    "source": "y = 2",
                    "outputs": [
                        {"output_type": "error", "ename": "ValueError", "evalue": "err"}
                    ],
                },
            ]
        }
        mock_result = Mock()
        mock_result.content = [session.TextContent(type="text", text=json.dumps(raw_cells_response))]
        mock_result.structuredContent = {}
        mock_proxy_client.proxy_mcp_client.call_tool_mcp.return_value = mock_result

        tools = {
            tool.name: tool
            for tool in session.make_static_colab_proxy_tools(mock_proxy_client)
        }
        get_cells_tool = tools["get_cells"]

        # Call with empty arguments
        result = await get_cells_tool.run({})

        # Verify upstream was called with includeOutputs: False
        mock_proxy_client.proxy_mcp_client.call_tool_mcp.assert_called_with(
            "get_cells", {"includeOutputs": False}
        )

        # Verify returned JSON has outputs stripped, source flattened, and hasOutputs/outputCount/hasError added
        parsed = json.loads(result.content[0].text)
        cells = parsed["cells"]
        assert len(cells) == 2

        c1 = cells[0]
        assert "outputs" not in c1
        assert c1["source"] == "x = 1\nprint(x)\n"
        assert c1["hasOutputs"] is True
        assert c1["outputCount"] == 1
        assert "hasError" not in c1

        c2 = cells[1]
        assert "outputs" not in c2
        assert c2["hasOutputs"] is True
        assert c2["outputCount"] == 1
        assert c2["hasError"] is True

        assert parsed["includeOutputs"] is False
        assert "hint" in parsed

    @pytest.mark.asyncio
    async def test_get_cells_preserves_outputs_when_requested(self, mock_proxy_client):
        mock_proxy_client.is_connected.return_value = True
        mock_proxy_client.proxy_mcp_client = AsyncMock()
        raw_cells_response = {
            "cells": [
                {
                    "id": "c1",
                    "cell_type": "code",
                    "source": "print(1)",
                    "outputs": [{"output_type": "stream", "text": "1\n"}],
                }
            ]
        }
        mock_result = Mock()
        mock_result.content = [session.TextContent(type="text", text=json.dumps(raw_cells_response))]
        mock_result.structuredContent = {}
        mock_proxy_client.proxy_mcp_client.call_tool_mcp.return_value = mock_result

        tools = {
            tool.name: tool
            for tool in session.make_static_colab_proxy_tools(mock_proxy_client)
        }
        get_cells_tool = tools["get_cells"]

        result = await get_cells_tool.run({"includeOutputs": True})
        mock_proxy_client.proxy_mcp_client.call_tool_mcp.assert_called_with(
            "get_cells", {"includeOutputs": True}
        )
        parsed = json.loads(result.content[0].text)
        c1 = parsed["cells"][0]
        assert "outputs" in c1
        assert c1["hasOutputs"] is True
        assert parsed["includeOutputs"] is True
