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

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
import asyncio
import json
import logging
import mcp.types as types
import os
from pathlib import Path
from mcp.shared.message import SessionMessage
from pydantic_core import ValidationError
import secrets
import tempfile
import urllib.parse
import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol


COLAB = "https://colab.research.google.com"
COLAB_ALT_DOMAIN = "https://colab.google.com"
SCRATCH_PATH = "/notebooks/empty.ipynb"
STATE_PATH = Path(tempfile.gettempdir()) / "colab-mcp-current.json"


class ColabWebSocketServer:
    """
    A WebSocket server designed to accept a single connection specifically
    from a Google Colab session (colab.google.com).
    """

    def __init__(self, host="127.0.0.1"):
        self.host = host
        self.port = 0
        self.connection_lock = asyncio.Lock()
        self.connection_live = asyncio.Event()
        self.connection_generation = 0
        self._connection_count = 0
        self.allowed_origins = [COLAB, COLAB_ALT_DOMAIN]
        self._server: websockets.Server | None = None

        self.read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
        self._read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
        self.write_stream: MemoryObjectSendStream[SessionMessage]
        self._write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

        self._read_stream_writer, self.read_stream = anyio.create_memory_object_stream(
            0
        )
        self.write_stream, self._write_stream_reader = (
            anyio.create_memory_object_stream(0)
        )
        self.token = secrets.token_urlsafe(16)

    def _reset_streams(self):
        """Safely closes existing streams and re-creates clean memory streams for new connections."""
        try:
            self._read_stream_writer.close()
        except Exception:
            pass
        try:
            self.read_stream.close()
        except Exception:
            pass
        try:
            self.write_stream.close()
        except Exception:
            pass
        try:
            self._write_stream_reader.close()
        except Exception:
            pass
        self._read_stream_writer, self.read_stream = anyio.create_memory_object_stream(0)
        self.write_stream, self._write_stream_reader = anyio.create_memory_object_stream(0)

    def connection_info(self) -> dict:
        scratch_url = (
            f"{COLAB}{SCRATCH_PATH}#mcpProxyToken={self.token}&mcpProxyPort={self.port}"
        )
        return {
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "scratchUrl": scratch_url,
            "pid": os.getpid(),
        }

    def _write_state_file(self):
        STATE_PATH.write_text(
            json.dumps(self.connection_info(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _remove_state_file(self):
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if state.get("pid") == os.getpid() and state.get("port") == self.port:
            try:
                STATE_PATH.unlink()
            except FileNotFoundError:
                pass

    async def _read_from_socket(self, websocket):
        """Listens to the socket and puts messages into the read stream."""
        async for msg in websocket:
            try:
                client_message = types.JSONRPCMessage.model_validate_json(msg)
            except ValidationError as exc:
                await self._read_stream_writer.send(exc)
                continue
            await self._read_stream_writer.send(SessionMessage(client_message))

    async def _write_to_socket(self, websocket):
        """Reads from the write stream and sends over the socket."""
        try:
            while True:
                # Wait for a message from the application
                msg = await self._write_stream_reader.receive()

                try:
                    json_obj = msg.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await websocket.send(json_obj)
                except ConnectionClosed:
                    return "socket_closed"
        except (anyio.ClosedResourceError, anyio.EndOfStream):
            # server closed write stream
            return "write_stream_closed"

    def _notify_read_exception(self, exc: Exception) -> None:
        try:
            self._read_stream_writer.send_nowait(exc)
        except anyio.WouldBlock:
            logging.debug("Dropping read-stream exception because no receiver is ready: %s", exc)
        except anyio.ClosedResourceError:
            pass

    def _validate_authorization(self, websocket: ServerConnection, request: Request):
        query = urllib.parse.urlsplit(request.path).query
        params = urllib.parse.parse_qs(query)
        query_tokens = params.get("access_token", [])
        if any(secrets.compare_digest(token, self.token) for token in query_tokens):
            return None
        try:
            headers: Headers = request.headers
            auth_header = headers.get("Authorization")
            if not auth_header:
                return Response(401, "Missing authorization", Headers([]))
            scheme, token = auth_header.split(None, 1)
            if scheme.lower() != "bearer":
                return Response(400, "Invalid authorization header", Headers([]))
        except ValueError:
            return Response(400, "Invalid header format", Headers([]))
        if secrets.compare_digest(token, self.token):
            return None
        return Response(403, "Bad authorization token", Headers([]))

    async def _connection_handler(self, websocket: ServerConnection):
        """
        Handles incoming websocket connections.
        Validates Origin and ensures single-client exclusivity.
        """
        if self.connection_lock.locked():
            logging.warning(
                f"Connection rejected: {websocket.remote_address}. A client is already connected"
            )
            await websocket.close(code=1013, reason="Server is busy")
            return

        async with self.connection_lock:
            if self._connection_count > 0:
                self._reset_streams()
            self._connection_count += 1
            self.connection_generation += 1
            try:
                self.connection_live.set()

                reading_task = asyncio.create_task(self._read_from_socket(websocket))
                writing_task = asyncio.create_task(self._write_to_socket(websocket))
                done, pending = await asyncio.wait(
                    [reading_task, writing_task], return_when=asyncio.FIRST_COMPLETED
                )

                disconnected = False
                for task in done:
                    try:
                        result = task.result()
                    except websockets.exceptions.ConnectionClosed as e:
                        logging.info(f"Connection closed: {e.code} - {e.reason}")
                        disconnected = True
                    except Exception as e:
                        logging.exception("WebSocket worker failed")
                        self._notify_read_exception(e)
                    else:
                        disconnected = disconnected or task is reading_task
                        disconnected = disconnected or result == "socket_closed"

                if disconnected:
                    self._notify_read_exception(Exception("Colab Frontend disconnected"))

                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            except websockets.exceptions.ConnectionClosed as e:
                logging.info(f"Connection closed: {e.code} - {e.reason}")
                self._notify_read_exception(Exception("Colab Frontend disconnected"))
            except Exception as e:
                logging.exception("Unexpected error")
                self._notify_read_exception(e)
            finally:
                self.connection_live.clear()

    async def __aenter__(self):
        self._server = await websockets.serve(
            self._connection_handler,
            host=self.host,
            port=0,
            subprotocols=[Subprotocol("mcp")],
            origins=self.allowed_origins,
            process_request=self._validate_authorization,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self._write_state_file()
        logging.info(f"Starting WebSocket server on ws://{self.host}:{self.port}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logging.info("Closing WebSocket server")
        if self._server:
            self._server.close()
            self.write_stream.close()
            self.read_stream.close()
            await self._server.wait_closed()
        self._remove_state_file()
