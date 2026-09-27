#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""MCP server: thin adapter over the shared Blender Android RPC client.

Each tool validates its arguments and then calls the same
:class:`BlenderControlClient` used by ``blenderctl``. No Blender-specific
business logic lives here, and there is intentionally no generic
``execute_python`` or shell tool.

Requires the official MCP Python SDK v1 API (``mcp<2``, FastMCP)::

    uvx --python 3.12 --with "mcp<2" <path-to>/mcp_server.py

Configuration via environment (same names as the CLI)::

    BLENDER_CONTROL_HOST / BLENDER_CONTROL_PORT / BLENDER_CONTROL_TOKEN

Transport notes: same-device agents talk to 127.0.0.1:<port> directly;
external hosts use ``adb forward tcp:<host-port> tcp:<device-port>``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import BlenderControlClient, RpcError  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "mcp_server: the official MCP SDK is required (pip install 'mcp<2'): %s" % e)

ALLOWED_CREATE_TYPES = ("CUBE", "UV_SPHERE")
ALLOWED_RENDER_ENGINES = ("BLENDER_EEVEE",)

server = FastMCP("blender-android-control")


def _client():
    token = os.environ.get("BLENDER_CONTROL_TOKEN", "")
    if not token:
        raise ValueError("BLENDER_CONTROL_TOKEN is not set")
    return BlenderControlClient.from_env()


def _wrap(fn, *args, **kwargs):
    try:
        return {"ok": True, "result": fn(*args, **kwargs)}
    except RpcError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message}}
    except ValueError as e:
        return {"ok": False, "error": {"code": "INVALID_PARAMS", "message": str(e)}}


def _check_vec3(value, field):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s must be a list of 3 numbers" % field)
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        raise ValueError("%s must be a list of 3 numbers" % field)


@server.tool()
def blender_ping() -> dict:
    """Probe the bridge: protocol, Blender version, platform, main-thread state."""
    c = _client()
    return _wrap(c.ping)


@server.tool()
def blender_scene_inspect() -> dict:
    """Return structured scene state (objects, transforms, engine)."""
    c = _client()
    return _wrap(c.scene_inspect)


@server.tool()
def blender_object_create(primitive: str, name: str = "",
                          location: list = None, rotation: list = None,
                          scale: list = None) -> dict:
    """Create a primitive. primitive is CUBE or UV_SPHERE; name is optional."""
    ctype = (primitive or "").upper().replace("-", "_")
    if ctype not in ALLOWED_CREATE_TYPES:
        return {"ok": False, "error": {
            "code": "INVALID_PARAMS",
            "message": "primitive must be one of %s" % list(ALLOWED_CREATE_TYPES)}}
    c = _client()
    return _wrap(c.object_create, ctype,
                 name=name or None,
                 location=_check_vec3(location, "location"),
                 rotation=_check_vec3(rotation, "rotation"),
                 scale=_check_vec3(scale, "scale"))


@server.tool()
def blender_object_transform(name: str, location: list = None,
                             rotation: list = None, scale: list = None) -> dict:
    """Set location/rotation/scale of a named object; returns final transform."""
    if not name:
        return {"ok": False, "error": {"code": "INVALID_PARAMS",
                                       "message": "name is required"}}
    if location is None and rotation is None and scale is None:
        return {"ok": False, "error": {"code": "INVALID_PARAMS",
                                       "message": "nothing to change"}}
    c = _client()
    return _wrap(c.object_transform, name,
                 location=_check_vec3(location, "location"),
                 rotation=_check_vec3(rotation, "rotation"),
                 scale=_check_vec3(scale, "scale"))


@server.tool()
def blender_object_delete(name: str) -> dict:
    """Delete one named object."""
    if not name:
        return {"ok": False, "error": {"code": "INVALID_PARAMS",
                                       "message": "name is required"}}
    c = _client()
    return _wrap(c.object_delete, name)


@server.tool()
def blender_scene_save(path: str) -> dict:
    """Save the .blend to an explicit absolute path."""
    if not path or not os.path.isabs(path):
        return {"ok": False, "error": {"code": "INVALID_PARAMS",
                                       "message": "path must be absolute"}}
    c = _client()
    return _wrap(c.scene_save, path)


@server.tool()
def blender_render_still(output: str, engine: str = "BLENDER_EEVEE",
                         resolution_x: int = 256, resolution_y: int = 256,
                         percentage: int = 100) -> dict:
    """Minimal deterministic Eevee still render to an absolute output path."""
    if not output or not os.path.isabs(output):
        return {"ok": False, "error": {"code": "INVALID_PARAMS",
                                       "message": "output must be absolute"}}
    if engine not in ALLOWED_RENDER_ENGINES:
        return {"ok": False, "error": {
            "code": "INVALID_PARAMS",
            "message": "engine must be one of %s" % list(ALLOWED_RENDER_ENGINES)}}
    c = _client()
    return _wrap(c.render_still, output, engine=engine,
                 resolution_x=resolution_x, resolution_y=resolution_y,
                 percentage=percentage)


def main():
    server.run()


if __name__ == "__main__":
    main()
