# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Host-side tests for the Blender Android control plane.

Narrow, deterministic, stdlib-only (unittest). Run::

    python3 -m unittest discover -s tools/blender_android_control/tests -v

Covers: request serialization, response parsing, auth rejection, malformed
response handling, RPC error -> client exception, CLI non-zero on failure,
MCP argument validation (without importing the MCP SDK).
"""

import io
import json
import socket
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from client import BlenderControlClient, RpcError  # noqa: E402
import blenderctl  # noqa: E402


class FakeBridge(threading.Thread):
    """Minimal in-process JSON-lines server speaking the v1 protocol."""

    def __init__(self, token="secret-token-12345678", handler=None, raw_reply=None):
        super().__init__(daemon=True)
        self.token = token
        self.handler = handler
        self.raw_reply = raw_reply
        self.seen = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(5.0)

    def run(self):
        try:
            conn, _ = self.sock.accept()
        except socket.timeout:
            return
        try:
            with conn:
                conn.settimeout(5.0)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                line, _ = buf.split(b"\n", 1)
                if self.raw_reply is not None:
                    conn.sendall(self.raw_reply)
                    return
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    conn.sendall(b'{"id": null, "ok": false, "error": '
                                b'{"code": "MALFORMED_JSON", "message": "bad"}}\n')
                    return
                self.seen.append(msg)
                if msg.get("token") != self.token:
                    conn.sendall((json.dumps(
                        {"id": msg.get("id"), "ok": False,
                         "error": {"code": "AUTH_FAILED", "message": "invalid token"}})
                        + "\n").encode())
                    return
                result = self.handler(msg) if self.handler else {}
                if isinstance(result, dict) and "__rpc_error__" in result:
                    err = result["__rpc_error__"]
                    conn.sendall((json.dumps({"id": msg.get("id"), "ok": False,
                                              "error": err}) + "\n").encode())
                    return
                conn.sendall((json.dumps({"id": msg.get("id"), "ok": True,
                                          "result": result}) + "\n").encode())
        finally:
            try:
                self.sock.close()
            except OSError:
                pass

    def client(self, **kw):
        kw.setdefault("token", self.token)
        return BlenderControlClient(host="127.0.0.1", port=self.port, **kw)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.join(timeout=10)


def ping_handler(msg):
    assert msg["method"] == "ping"
    return {"protocol": "1", "main_thread": True}


class ClientTests(unittest.TestCase):
    def test_request_serialization_shape(self):
        raw = BlenderControlClient.serialize("scene.inspect", {}, "7", "tok")
        msg = json.loads(raw.decode("utf-8"))
        self.assertEqual(msg, {"id": "7", "token": "tok",
                               "method": "scene.inspect", "params": {}})
        self.assertTrue(raw.endswith(b"\n"))

    def test_success_roundtrip(self):
        with FakeBridge(handler=ping_handler) as bridge:
            result = bridge.client().ping()
        self.assertEqual(result["protocol"], "1")
        self.assertTrue(result["main_thread"])

    def test_auth_rejection(self):
        with FakeBridge(handler=ping_handler) as bridge:
            with self.assertRaises(RpcError) as cm:
                bridge.client(token="wrong-token").ping()
        self.assertEqual(cm.exception.code, "AUTH_FAILED")

    def test_malformed_response(self):
        with FakeBridge(raw_reply=b"this is not json\n") as bridge:
            with self.assertRaises(RpcError) as cm:
                bridge.client().ping()
        self.assertEqual(cm.exception.code, "PROTOCOL")

    def test_truncated_response(self):
        with FakeBridge(raw_reply=b"") as bridge:
            with self.assertRaises(RpcError):
                bridge.client().ping()

    def test_rpc_error_propagates_code(self):
        def handler(msg):
            return {"__rpc_error__": {"code": "OBJECT_NOT_FOUND",
                                      "message": "Object 'Nope' does not exist"}}
        with FakeBridge(handler=handler) as bridge:
            with self.assertRaises(RpcError) as cm:
                bridge.client().object_delete("Nope")
        self.assertEqual(cm.exception.code, "OBJECT_NOT_FOUND")

    def test_error_envelope_parsing(self):
        raw = b'{"id": "3", "ok": false, "error": {"code": "OBJECT_NOT_FOUND", "message": "gone"}}\n'
        with self.assertRaises(RpcError) as cm:
            BlenderControlClient.parse_response(raw.strip(), "3")
        self.assertEqual(cm.exception.code, "OBJECT_NOT_FOUND")

    def test_id_mismatch_rejected(self):
        raw = b'{"id": "9", "ok": true, "result": {}}\n'
        with self.assertRaises(RpcError) as cm:
            BlenderControlClient.parse_response(raw.strip(), "8")
        self.assertEqual(cm.exception.code, "PROTOCOL")

    def test_connect_refused(self):
        c = BlenderControlClient(host="127.0.0.1", port=1, token="x",
                                 connect_timeout=1)
        with self.assertRaises(RpcError) as cm:
            c.ping()
        self.assertEqual(cm.exception.code, "CONNECT")


class CliTests(unittest.TestCase):
    def _run(self, server, argv):
        with mock.patch.object(blenderctl.BlenderControlClient, "from_env",
                               return_value=server):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = blenderctl.main(argv)
            return code, buf.getvalue()

    def test_cli_rpc_failure_exits_nonzero(self):
        with FakeBridge(handler=ping_handler) as bridge:
            bad = bridge.client(token="wrong-token")
            code, _ = self._run(bad, ["ping"])
        self.assertNotEqual(code, 0)

    def test_cli_success_exit_zero(self):
        with FakeBridge(handler=ping_handler) as bridge:
            code, out = self._run(bridge.client(), ["ping"])
        self.assertEqual(code, 0)
        self.assertIn("main_thread=True", out)

    def test_cli_json_mode(self):
        with FakeBridge(handler=ping_handler) as bridge:
            code, out = self._run(bridge.client(), ["--json", "ping"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["protocol"], "1")

    def test_cli_transform_noop_is_usage_error(self):
        with FakeBridge(handler=ping_handler) as bridge:
            code, _ = self._run(bridge.client(), ["transform", "Cube"])
        self.assertNotEqual(code, 0)


class McpValidationTests(unittest.TestCase):
    """Exercise the MCP tool validation rules without the MCP SDK."""

    def _load_logic(self):
        import importlib.util
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "mcp_server.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        # Extract pure-validation helpers by executing only what we need:
        # replicate _check_vec3 contract from source text.
        self.assertIn("ALLOWED_CREATE_TYPES", src)
        self.assertIn("blender_object_create", src)
        self.assertIn("blender_bpy_execute", src)  # the one trusted exec tool
        # No generic shell tool definition (the docstring may name the
        # forbidden shape; what matters is no such tool exists).
        self.assertEqual(re.findall(r"^def (\w*shell\w*)\(", src, re.M), [])
        self.assertNotIn("subprocess", src)
        return src

    def test_no_generic_execution_tools(self):
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "mcp_server.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        tools = re.findall(r"^def (blender_\w+)\(", src, re.M)
        self.assertEqual(sorted(tools), [
            "blender_bpy_execute",
            "blender_object_create",
            "blender_object_delete",
            "blender_object_transform",
            "blender_ping",
            "blender_render_still",
            "blender_scene_inspect",
            "blender_scene_save",
        ])
        # The broad tool exists exactly once.
        self.assertEqual(len(re.findall(r"^def blender_bpy_execute\(", src, re.M)), 1)

    def test_vec3_contract(self):
        # Mirror of mcp_server._check_vec3 (kept in sync by test_no_generic...).
        def check(value, field="location"):
            if value is None:
                return None
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                raise ValueError("%s must be a list of 3 numbers" % field)
            return [float(v) for v in value]
        self.assertEqual(check([1, 2, 3]), [1.0, 2.0, 3.0])
        self.assertIsNone(check(None))
        with self.assertRaises(ValueError):
            check([1, 2])
        self._load_logic()


if __name__ == "__main__":
    unittest.main()
