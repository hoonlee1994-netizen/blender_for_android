# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Shared localhost RPC client for the Blender Android control plane (v1).

Single transport implementation used by both ``blenderctl`` (CLI) and the
MCP server. Do not duplicate socket/protocol code elsewhere: import this.

Protocol: newline-delimited UTF-8 JSON over TCP 127.0.0.1. One request per
line, one response per line::

    {"id": "1", "token": "<secret>", "method": "scene.inspect", "params": {}}
"""

import itertools
import json
import os
import socket

PROTOCOL_VERSION = "1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17878
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0
RENDER_READ_TIMEOUT = 200.0
MAX_RESPONSE_BYTES = 1024 * 1024

ENV_HOST = "BLENDER_CONTROL_HOST"
ENV_PORT = "BLENDER_CONTROL_PORT"
ENV_TOKEN = "BLENDER_CONTROL_TOKEN"


class RpcError(Exception):
    """A structured failure from the Blender control bridge."""

    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


_id_counter = itertools.count(1)


def load_config_file(path):
    """Read a local control-config JSON file ({token, port}) for --config."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    token = data.get("token", "")
    if not isinstance(token, str) or not token:
        raise ValueError("config is missing a token")
    port = data.get("port", DEFAULT_PORT)
    return token, int(port)


class BlenderControlClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, token="",
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 read_timeout=DEFAULT_READ_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.token = token
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    @classmethod
    def from_env(cls, host=None, port=None, token=None, config=None, **kw):
        if config:
            file_token, file_port = load_config_file(config)
            token = token or file_token
            port = port if port is not None else file_port
        return cls(
            host=host or os.environ.get(ENV_HOST, DEFAULT_HOST),
            port=port if port is not None else int(os.environ.get(ENV_PORT, DEFAULT_PORT)),
            token=token or os.environ.get(ENV_TOKEN, ""),
            **kw,
        )

    def _build_request(self, method, params, req_id):
        return {
            "id": req_id if req_id is not None else str(next(_id_counter)),
            "token": self.token,
            "method": method,
            "params": params if params is not None else {},
        }

    @staticmethod
    def serialize(method, params, req_id, token):
        return (json.dumps({
            "id": req_id, "token": token, "method": method, "params": params,
        }) + "\n").encode("utf-8")

    @staticmethod
    def parse_response(raw, req_id):
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RpcError("PROTOCOL", "response is not valid JSON")
        if not isinstance(msg, dict) or "ok" not in msg:
            raise RpcError("PROTOCOL", "response is not a valid RPC envelope")
        if msg.get("id") != req_id:
            raise RpcError("PROTOCOL", "response id mismatch")
        if msg.get("ok") is True:
            return msg.get("result", {})
        err = msg.get("error", {})
        if not isinstance(err, dict):
            raise RpcError("PROTOCOL", "error payload malformed")
        raise RpcError(err.get("code", "UNKNOWN"), err.get("message", "unknown error"))

    def request(self, method, params=None, req_id=None, timeout=None):
        req_id = req_id if req_id is not None else str(next(_id_counter))
        payload = self.serialize(method, params if params is not None else {},
                                 req_id, self.token)
        read_timeout = timeout if timeout is not None else self.read_timeout
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.connect_timeout)
        except OSError as e:
            raise RpcError("CONNECT", "cannot connect to %s:%d (%s)"
                           % (self.host, self.port, e))
        try:
            sock.settimeout(read_timeout)
            try:
                sock.sendall(payload)
            except OSError as e:
                raise RpcError("TRANSPORT", "send failed (%s)" % e)
            buf = b""
            while b"\n" not in buf:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    raise RpcError("TIMEOUT", "no response within %.0fs" % read_timeout)
                except OSError as e:
                    raise RpcError("TRANSPORT", "recv failed (%s)" % e)
                if not chunk:
                    raise RpcError("PROTOCOL", "connection closed before response")
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise RpcError("PROTOCOL", "response too large")
            line, _ = buf.split(b"\n", 1)
            return self.parse_response(line, req_id)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # -- convenience wrappers (thin; no Blender business logic) --------------

    def ping(self):
        return self.request("ping", {})

    def scene_inspect(self):
        return self.request("scene.inspect", {})

    def object_create(self, ctype, name=None, location=None, rotation=None, scale=None):
        params = {"type": ctype}
        if name is not None:
            params["name"] = name
        if location is not None:
            params["location"] = list(location)
        if rotation is not None:
            params["rotation"] = list(rotation)
        if scale is not None:
            params["scale"] = list(scale)
        return self.request("object.create", params)

    def object_transform(self, name, location=None, rotation=None, scale=None):
        params = {"name": name}
        if location is not None:
            params["location"] = list(location)
        if rotation is not None:
            params["rotation"] = list(rotation)
        if scale is not None:
            params["scale"] = list(scale)
        return self.request("object.transform", params)

    def object_delete(self, name):
        return self.request("object.delete", {"name": name})

    def scene_save(self, path):
        return self.request("scene.save", {"path": path})

    def render_still(self, output, engine="BLENDER_EEVEE",
                     resolution_x=256, resolution_y=256, percentage=100):
        return self.request("render.still", {
            "output": output, "engine": engine,
            "resolution_x": resolution_x, "resolution_y": resolution_y,
            "percentage": percentage,
        }, timeout=RENDER_READ_TIMEOUT)
