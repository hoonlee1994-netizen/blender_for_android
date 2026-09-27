# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Host-side tests for the trusted ``script.execute`` path (BPY-EXEC v1).

Narrow, deterministic, stdlib-only (unittest). Run::

    python3 -m unittest discover -s tools/blender_android_control/tests -v

Covers: config opt-in semantics (absent/false/malformed -> disabled),
FORBIDDEN dispatch when disabled, auth enforcement, handler validation,
source-size bound, result contract (result / stdout / duration_ms /
source_sha256), RESULT_NOT_SERIALIZABLE, EXECUTION_ERROR, bounded stdout,
fresh namespace per request, client transport, CLI exec plumbing, and the
MCP tool-surface contract (exactly one broad tool, no shell tool, the
seven semantic tools unchanged).

No bpy is required: the bridge module is imported directly and the script
handler is exercised with ordinary Python. Physical-device Blender behavior
remains authoritative for bpy itself.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from client import BlenderControlClient, RpcError  # noqa: E402
import blenderctl  # noqa: E402
import client as client_module  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                          "..", "..", ".."))
BRIDGE_PATH = os.path.join(REPO_ROOT, "scripts", "startup",
                           "bl_android_control.py")
MCP_PATH = os.path.join(os.path.dirname(__file__), "..", "mcp_server.py")

TEST_TOKEN = "test-token-12345678-abcdefgh"


def load_bridge():
    spec = importlib.util.spec_from_file_location("bl_android_control_test",
                                                  BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = load_bridge()

EXPECTED_SEMANTIC_METHODS = {
    "ping", "scene.inspect", "object.create", "object.transform",
    "object.delete", "scene.save", "render.still",
}

EXPECTED_MCP_TOOLS = sorted([
    "blender_ping",
    "blender_scene_inspect",
    "blender_object_create",
    "blender_object_transform",
    "blender_object_delete",
    "blender_scene_save",
    "blender_render_still",
    "blender_bpy_execute",
])


def write_config(data):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                    encoding="utf-8")
    try:
        json.dump(data, f)
        f.close()
        return f.name
    except Exception:
        os.unlink(f.name)
        raise


class ConfigOptInTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get(bridge.CONFIG_ENV_OVERRIDE)
        self._paths = []

    def tearDown(self):
        if self._old is None:
            os.environ.pop(bridge.CONFIG_ENV_OVERRIDE, None)
        else:
            os.environ[bridge.CONFIG_ENV_OVERRIDE] = self._old
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _load(self, data):
        path = write_config(data)
        self._paths.append(path)
        os.environ[bridge.CONFIG_ENV_OVERRIDE] = path
        return bridge._load_config()

    def test_absent_flag_means_disabled_but_bridge_enabled(self):
        config, status = self._load({"enabled": True, "token": TEST_TOKEN,
                                     "port": 17878})
        self.assertEqual(status, "enabled")
        self.assertIsNotNone(config)
        self.assertFalse(config["allow_script_execution"])

    def test_false_means_disabled(self):
        config, _ = self._load({"enabled": True, "token": TEST_TOKEN,
                                "port": 17878,
                                "allow_script_execution": False})
        self.assertFalse(config["allow_script_execution"])

    def test_true_means_enabled(self):
        config, status = self._load({"enabled": True, "token": TEST_TOKEN,
                                     "port": 17878,
                                     "allow_script_execution": True})
        self.assertEqual(status, "enabled")
        self.assertTrue(config["allow_script_execution"])

    def test_malformed_flag_means_disabled(self):
        for bad in ("yes", "true", 1, 0, None, [], {}, 1.0):
            config, status = self._load(
                {"enabled": True, "token": TEST_TOKEN, "port": 17878,
                 "allow_script_execution": bad})
            self.assertEqual(status, "enabled", bad)
            self.assertFalse(config["allow_script_execution"], bad)

    def test_bridge_defaults_to_disabled(self):
        b = bridge._Bridge(TEST_TOKEN, 17878)
        try:
            self.assertFalse(b._allow_script_execution)
        finally:
            b.stop()

    def test_bridge_strict_opt_in(self):
        for bad in (False, "yes", 1, None):
            b = bridge._Bridge(TEST_TOKEN, 17878,
                               allow_script_execution=bad)
            try:
                self.assertFalse(b._allow_script_execution, bad)
            finally:
                b.stop()
        b = bridge._Bridge(TEST_TOKEN, 17878, allow_script_execution=True)
        try:
            self.assertTrue(b._allow_script_execution)
        finally:
            b.stop()


class FakeConn:
    """Captures what the bridge would send on a socket."""

    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def replies(self):
        return [json.loads(d.decode("utf-8")) for d in self.sent]


def dispatch(b, method, params, token=TEST_TOKEN, req_id="s1"):
    conn = FakeConn()
    line = json.dumps({"id": req_id, "token": token, "method": method,
                       "params": params}).encode("utf-8")
    b._dispatch_line(conn, line)
    return conn.replies()


class DispatchGateTests(unittest.TestCase):
    def test_script_execute_forbidden_by_default(self):
        b = bridge._Bridge(TEST_TOKEN, 17878)
        try:
            replies = dispatch(b, "script.execute", {"source": "result = 1"})
        finally:
            b.stop()
        self.assertEqual(len(replies), 1)
        self.assertFalse(replies[0]["ok"])
        self.assertEqual(replies[0]["error"]["code"], "FORBIDDEN")

    def test_script_execute_forbidden_when_flag_absent(self):
        b = bridge._Bridge(TEST_TOKEN, 17878,
                           allow_script_execution=False)
        try:
            replies = dispatch(b, "script.execute", {"source": "result = 1"})
        finally:
            b.stop()
        self.assertEqual(replies[0]["error"]["code"], "FORBIDDEN")

    def test_wrong_token_rejected_even_when_enabled(self):
        b = bridge._Bridge(TEST_TOKEN, 17878, allow_script_execution=True)
        old = dict(bridge.HANDLERS)
        try:
            replies = dispatch(b, "script.execute", {"source": "result = 1"},
                               token="wrong-token")
        finally:
            bridge.HANDLERS.clear()
            bridge.HANDLERS.update(old)
            b.stop()
        self.assertEqual(replies[0]["error"]["code"], "AUTH_FAILED")

    def test_enabled_request_reaches_queue_not_forbidden(self):
        # Shorten the wait so the untestable timer path returns TIMEOUT
        # quickly instead of blocking for the 300s production bound.
        b = bridge._Bridge(TEST_TOKEN, 17878, allow_script_execution=True)
        entry = bridge.HANDLERS["script.execute"]
        bridge.HANDLERS["script.execute"] = (entry[0], 0.05)
        try:
            replies = dispatch(b, "script.execute", {"source": "result = 1"})
        finally:
            bridge.HANDLERS["script.execute"] = entry
            b.stop()
        # Not FORBIDDEN: queued, then the waiter timed out (no timer pump
        # in this host test; on-device the main-thread pump answers).
        self.assertEqual(replies[0]["error"]["code"], "TIMEOUT")

    def test_legacy_forbidden_prefixes_intact(self):
        b = bridge._Bridge(TEST_TOKEN, 17878, allow_script_execution=True)
        try:
            for method in ("python.eval", "python.exec", "os.system",
                           "shell.run", "subprocess.call", "exec"):
                replies = dispatch(b, method, {})
                self.assertEqual(replies[0]["error"]["code"], "FORBIDDEN",
                                 method)
        finally:
            b.stop()

    def test_semantic_surface_unchanged(self):
        self.assertEqual(set(bridge.HANDLERS.keys()),
                         EXPECTED_SEMANTIC_METHODS | {"script.execute"})
        self.assertEqual(bridge.HANDLERS["script.execute"][1],
                         bridge.SCRIPT_WAIT_TIMEOUT)


class HandlerContractTests(unittest.TestCase):
    def _err(self, params):
        res = bridge.h_script_execute(params, {})
        self.assertIn("__error__", res)
        return res["__error__"]

    def test_invalid_params_shape(self):
        self.assertEqual(self._err([])["code"], "INVALID_PARAMS")
        self.assertEqual(self._err({})["code"], "INVALID_PARAMS")
        self.assertEqual(self._err({"source": 123})["code"], "INVALID_PARAMS")
        self.assertEqual(self._err({"source": None})["code"], "INVALID_PARAMS")
        self.assertEqual(self._err({"source": ""})["code"], "INVALID_PARAMS")
        self.assertEqual(self._err({"source": ["result=1"]})["code"],
                         "INVALID_PARAMS")

    def test_invalid_label(self):
        self.assertEqual(
            self._err({"source": "result = 1", "label": 123})["code"],
            "INVALID_PARAMS")
        self.assertEqual(
            self._err({"source": "result = 1",
                       "label": "x" * 257})["code"],
            "INVALID_PARAMS")

    def test_source_size_bound(self):
        big = "x = 1  # pad\n" + "#" * bridge.SCRIPT_MAX_SOURCE_BYTES
        err = self._err({"source": big})
        self.assertEqual(err["code"], "SOURCE_TOO_LARGE")
        self.assertIn(str(bridge.SCRIPT_MAX_SOURCE_BYTES), err["message"])
        self.assertEqual(bridge.SCRIPT_MAX_SOURCE_BYTES, 256 * 1024)
        self.assertEqual(bridge.MAX_REQUEST_BYTES, 256 * 1024)

    def test_normal_result_return(self):
        source = "import math\nresult = {'objects_created': ['Example'], 'pi': math.pi}"
        res = bridge.h_script_execute({"source": source, "label": "demo"}, {})
        self.assertNotIn("__error__", res)
        self.assertEqual(res["result"],
                         {"objects_created": ["Example"], "pi": 3.141592653589793})
        self.assertIsInstance(res["stdout"], str)
        self.assertIsInstance(res["duration_ms"], int)
        self.assertGreaterEqual(res["duration_ms"], 0)
        self.assertEqual(res["source_sha256"],
                         hashlib.sha256(source.encode("utf-8")).hexdigest())
        self.assertEqual(set(res.keys()),
                         {"result", "stdout", "duration_ms", "source_sha256"})

    def test_missing_result_is_null(self):
        res = bridge.h_script_execute({"source": "x = 1 + 1"}, {})
        self.assertNotIn("__error__", res)
        self.assertIsNone(res["result"])

    def test_explicit_none_result(self):
        res = bridge.h_script_execute({"source": "result = None"}, {})
        self.assertNotIn("__error__", res)
        self.assertIsNone(res["result"])

    def test_non_serializable_result(self):
        err = self._err({"source": "result = object()"})
        self.assertEqual(err["code"], "RESULT_NOT_SERIALIZABLE")
        err = self._err({"source": "result = {1, 2, 3}"})
        self.assertEqual(err["code"], "RESULT_NOT_SERIALIZABLE")

    def test_execution_exception(self):
        err = self._err({"source": "raise ValueError('boom')"})
        self.assertEqual(err["code"], "EXECUTION_ERROR")
        self.assertIn("ValueError", err["message"])
        self.assertIn("boom", err["message"])

    def test_syntax_error_is_execution_error(self):
        err = self._err({"source": "def broken(:"})
        self.assertEqual(err["code"], "EXECUTION_ERROR")
        self.assertIn("SyntaxError", err["message"])

    def test_stdout_captured_and_bounded(self):
        res = bridge.h_script_execute(
            {"source": "print('hello')\nprint('world')"}, {})
        self.assertNotIn("__error__", res)
        self.assertIn("hello", res["stdout"])
        self.assertIn("world", res["stdout"])
        res = bridge.h_script_execute(
            {"source": "print('y' * 20000)"}, {})
        self.assertNotIn("__error__", res)
        self.assertLessEqual(len(res["stdout"]),
                             bridge.STDOUT_MAX_CHARS + 64)
        self.assertIn("truncated at %d chars" % bridge.STDOUT_MAX_CHARS,
                      res["stdout"])
        self.assertEqual(bridge.STDOUT_MAX_CHARS, 16 * 1024)

    def test_fresh_namespace_per_request(self):
        first = bridge.h_script_execute({"source": "result = {'a': 1}"}, {})
        self.assertEqual(first["result"], {"a": 1})
        second = bridge.h_script_execute({"source": "x = 2"}, {})
        self.assertNotIn("__error__", second)
        self.assertIsNone(second["result"])

    def test_timeout_ordering_documented(self):
        # Server bound < client default wait: the caller out-waits Blender.
        self.assertEqual(bridge.SCRIPT_WAIT_TIMEOUT, 300.0)
        self.assertGreater(client_module.SCRIPT_READ_TIMEOUT,
                           bridge.SCRIPT_WAIT_TIMEOUT)


class ScriptFakeBridge(threading.Thread):
    """Minimal JSON-lines server speaking the v1 protocol (script-aware)."""

    def __init__(self, token=TEST_TOKEN, handler=None):
        super().__init__(daemon=True)
        self.token = token
        self.handler = handler
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
                msg = json.loads(line.decode("utf-8"))
                self.seen.append(msg)
                if msg.get("token") != self.token:
                    conn.sendall((json.dumps(
                        {"id": msg.get("id"), "ok": False,
                         "error": {"code": "AUTH_FAILED",
                                   "message": "invalid token"}})
                        + "\n").encode())
                    return
                result = self.handler(msg) if self.handler else {}
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


class ClientScriptTests(unittest.TestCase):
    def test_script_execute_roundtrip(self):
        envelope = {"result": {"version": "5.3"},
                    "stdout": "", "duration_ms": 3,
                    "source_sha256": "abc"}
        with ScriptFakeBridge(handler=lambda msg: envelope) as b:
            out = b.client().script_execute("result = {'version': '5.3'}",
                                            label="probe")
        self.assertEqual(out, envelope)
        sent = b.seen[0]
        self.assertEqual(sent["method"], "script.execute")
        self.assertEqual(sent["params"]["source"],
                         "result = {'version': '5.3'}")
        self.assertEqual(sent["params"]["label"], "probe")

    def test_label_omitted_when_none(self):
        with ScriptFakeBridge(handler=lambda msg: {"result": None,
                                                   "stdout": "",
                                                   "duration_ms": 0,
                                                   "source_sha256": "x"}) as b:
            b.client().script_execute("pass")
        self.assertNotIn("label", b.seen[0]["params"])

    def test_auth_failure_propagates(self):
        with ScriptFakeBridge(handler=lambda msg: {}) as b:
            with self.assertRaises(RpcError) as cm:
                b.client(token="wrong").script_execute("result = 1")
        self.assertEqual(cm.exception.code, "AUTH_FAILED")

    def test_forbidden_propagates(self):
        # Realistic FORBIDDEN envelope through the parse path:
        raw = (b'{"id": "1", "ok": false, "error": {"code": "FORBIDDEN", '
               b'"message": "disabled"}}\n')
        with self.assertRaises(RpcError) as cm:
            BlenderControlClient.parse_response(raw.strip(), "1")
        self.assertEqual(cm.exception.code, "FORBIDDEN")

    def test_default_timeout_is_script_bound(self):
        with mock.patch.object(BlenderControlClient, "request",
                               return_value={}) as req:
            BlenderControlClient(host="127.0.0.1", port=1,
                                 token="t").script_execute("result = 1")
            _, kw = req.call_args
            self.assertEqual(kw.get("timeout"),
                             client_module.SCRIPT_READ_TIMEOUT)


class CliExecTests(unittest.TestCase):
    def _run(self, client_obj, argv, stdin_data=None):
        with mock.patch.object(blenderctl.BlenderControlClient, "from_env",
                               return_value=client_obj):
            buf = io.StringIO()
            if stdin_data is not None:
                with mock.patch.object(sys, "stdin",
                                       io.StringIO(stdin_data)):
                    with redirect_stdout(buf):
                        code = blenderctl.main(argv)
            else:
                with redirect_stdout(buf):
                    code = blenderctl.main(argv)
            return code, buf.getvalue()

    def test_exec_inline_source_json(self):
        envelope = {"result": {"n": 1}, "stdout": "",
                    "duration_ms": 1, "source_sha256": "s"}
        fake = mock.Mock()
        fake.script_execute.return_value = envelope
        code, out = self._run(fake, ["--json", "exec", "--source",
                                     "result = {'n': 1}"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), envelope)
        fake.script_execute.assert_called_once_with(
            "result = {'n': 1}", label=None, timeout=None)

    def test_exec_file_and_label(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as f:
            f.write("result = {'from': 'file'}")
            path = f.name
        try:
            fake = mock.Mock()
            fake.script_execute.return_value = {"result": None, "stdout": "",
                                                "duration_ms": 0,
                                                "source_sha256": "s"}
            code, _ = self._run(fake, ["exec", "--file", path,
                                       "--label", "file-task"])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        fake.script_execute.assert_called_once_with(
            "result = {'from': 'file'}", label="file-task", timeout=None)

    def test_exec_stdin(self):
        fake = mock.Mock()
        fake.script_execute.return_value = {"result": None, "stdout": "hi",
                                            "duration_ms": 0,
                                            "source_sha256": "s"}
        code, _ = self._run(fake, ["exec", "--stdin"],
                            stdin_data="print('hi')")
        self.assertEqual(code, 0)
        fake.script_execute.assert_called_once_with(
            "print('hi')", label=None, timeout=None)

    def test_exec_rpc_failure_exits_nonzero(self):
        fake = mock.Mock()
        fake.script_execute.side_effect = RpcError("FORBIDDEN", "disabled")
        code, _ = self._run(fake, ["exec", "--source", "result = 1"])
        self.assertNotEqual(code, 0)

    def test_existing_commands_still_registered(self):
        self.assertEqual(sorted(blenderctl.COMMANDS.keys()), sorted(
            ["ping", "scene", "create", "transform", "delete", "save",
             "render", "exec", "gen-token"]))


class McpSurfaceTests(unittest.TestCase):
    def _src(self):
        with open(MCP_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def test_tool_names(self):
        src = self._src()
        tools = re.findall(r"^def (blender_\w+)\(", src, re.M)
        self.assertEqual(sorted(tools), EXPECTED_MCP_TOOLS)

    def test_broad_tool_exists_exactly_once(self):
        src = self._src()
        self.assertEqual(
            len(re.findall(r"^def blender_bpy_execute\(", src, re.M)), 1)
        self.assertIn("script_execute", src)

    def test_no_shell_tool(self):
        src = self._src()
        # No shell tool *definition* (the docstring names the forbidden
        # shape explicitly; what matters is no such tool exists).
        shell_tools = re.findall(r"^def (\w*shell\w*)\(", src, re.M)
        self.assertEqual(shell_tools, [])
        self.assertNotIn("subprocess", src)
        self.assertNotIn("os.system", src)

    def test_seven_semantic_tools_remain(self):
        src = self._src()
        for name in EXPECTED_MCP_TOOLS:
            if name == "blender_bpy_execute":
                continue
            self.assertIn("def %s(" % name, src)

    def test_no_bpy_wrappers_explosion(self):
        src = self._src()
        tools = re.findall(r"^def (blender_\w+)\(", src, re.M)
        self.assertEqual(len(tools), 8)


if __name__ == "__main__":
    unittest.main()
