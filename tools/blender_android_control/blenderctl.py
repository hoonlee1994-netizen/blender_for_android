#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""blenderctl -- CLI for the Blender Android control plane (Gate A).

Configuration (no source edits needed)::

    --host/--port/--token flags, or
    BLENDER_CONTROL_HOST / BLENDER_CONTROL_PORT / BLENDER_CONTROL_TOKEN,
    or --config <local blender-control.json> (token + port).

Transport:

* same-device agent (Termux): direct 127.0.0.1:<port>
* external host: ``adb forward tcp:<host-port> tcp:<device-port>`` first,
  then point --host/--port at the forwarded port. Never expose Blender to LAN.
"""

import argparse
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import BlenderControlClient, RpcError  # noqa: E402


def _parse_vec3(text, field):
    try:
        parts = [float(x) for x in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("%s must be x,y,z numbers" % field)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("%s must be x,y,z numbers" % field)
    return parts


def _parse_resolution(text):
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError("resolution must be WxH, e.g. 256x256")


def _build_client(args):
    try:
        return BlenderControlClient.from_env(
            host=args.host, port=args.port, token=args.token, config=args.config)
    except (ValueError, OSError) as e:
        print("blenderctl: bad configuration: %s" % e, file=sys.stderr)
        raise SystemExit(1)


def _emit(args, result):
    if args.json:
        print(json.dumps(result, indent=2))
    return result


def cmd_ping(client, args):
    r = _emit(args, client.ping())
    if not args.json:
        print("protocol=%s blender=%s (%s) platform=%s pid=%s main_thread=%s" % (
            r.get("protocol"), r.get("blender_version_string", r.get("blender_version")),
            r.get("blender_version"), r.get("platform"), r.get("pid"),
            r.get("main_thread")))
    return 0


def cmd_scene(client, args):
    r = _emit(args, client.scene_inspect())
    if not args.json:
        print("scene=%s engine=%s active=%s objects=%d%s" % (
            r.get("scene"), r.get("engine"), r.get("active_object"),
            r.get("object_count"), " (truncated)" if r.get("truncated") else ""))
        for o in r.get("objects", []):
            print("  %-24s %-10s loc=%s rot=%s scale=%s" % (
                o["name"], o["type"], o["location"], o["rotation"], o["scale"]))
    return 0


def _transform_kwargs(args):
    kw = {}
    if args.location is not None:
        kw["location"] = args.location
    if args.rotation is not None:
        kw["rotation"] = args.rotation
    if args.scale is not None:
        kw["scale"] = args.scale
    return kw


def cmd_create(client, args):
    ctype = {"cube": "CUBE", "uv-sphere": "UV_SPHERE"}[args.primitive]
    r = _emit(args, client.object_create(ctype, name=args.name, **_transform_kwargs(args)))
    if not args.json:
        print("created %s loc=%s rot=%s scale=%s" % (
            r.get("name"), r.get("location"), r.get("rotation"), r.get("scale")))
    return 0


def cmd_transform(client, args):
    kw = _transform_kwargs(args)
    if not kw:
        print("blenderctl: nothing to change (pass --location/--rotation/--scale)",
              file=sys.stderr)
        return 1
    r = _emit(args, client.object_transform(args.name, **kw))
    if not args.json:
        print("transformed %s loc=%s rot=%s scale=%s" % (
            r.get("name"), r.get("location"), r.get("rotation"), r.get("scale")))
    return 0


def cmd_delete(client, args):
    r = _emit(args, client.object_delete(args.name))
    if not args.json:
        print("deleted %s" % r.get("deleted"))
    return 0


def cmd_save(client, args):
    r = _emit(args, client.scene_save(args.path))
    if not args.json:
        print("saved %s" % r.get("path"))
    return 0


def cmd_render(client, args):
    w, h = args.resolution
    r = _emit(args, client.render_still(args.output, engine=args.engine,
                                        resolution_x=w, resolution_y=h,
                                        percentage=args.percentage))
    if not args.json:
        print("rendered %s engine=%s %dx%d" % (
            r.get("output"), r.get("engine"), r.get("width"), r.get("height")))
    return 0


def cmd_gen_token(_client, args):
    del _client
    print(secrets.token_hex(32))
    return 0


def _read_exec_source(args):
    if args.file is not None:
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read()
    if args.stdin:
        return sys.stdin.read()
    return args.source


def cmd_exec(client, args):
    try:
        source = _read_exec_source(args)
    except OSError as e:
        print("blenderctl: cannot read script source (%s)" % e, file=sys.stderr)
        return 1
    if not source:
        print("blenderctl: empty script source", file=sys.stderr)
        return 1
    r = _emit(args, client.script_execute(
        source, label=args.label,
        timeout=args.timeout if args.timeout else None))
    if not args.json:
        print("ok sha=%s duration_ms=%s" % (
            r.get("source_sha256"), r.get("duration_ms")))
        result = r.get("result")
        if result is not None:
            print(json.dumps(result, indent=2)[:4000])
        if r.get("stdout"):
            print("--- script stdout (bounded) ---")
            print(r["stdout"][:4000])
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="blenderctl",
                                description="Control Blender on Android over localhost RPC")
    p.add_argument("--host", default=None, help="bridge host (default 127.0.0.1)")
    p.add_argument("--port", default=None, type=int, help="bridge port (default 17878)")
    p.add_argument("--token", default=None, help="bridge auth token")
    p.add_argument("--config", default=None,
                   help="local control-config JSON file (token + port)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="bridge / version / main-thread probe")

    sub.add_parser("scene", help="structured scene inspection")

    c = sub.add_parser("create", help="create a primitive object")
    c.add_argument("primitive", choices=["cube", "uv-sphere"])
    c.add_argument("--name", default=None)
    c.add_argument("--location", default=None, type=lambda s: _parse_vec3(s, "location"))
    c.add_argument("--rotation", default=None, type=lambda s: _parse_vec3(s, "rotation"))
    c.add_argument("--scale", default=None, type=lambda s: _parse_vec3(s, "scale"))

    t = sub.add_parser("transform", help="set an object's transform")
    t.add_argument("name")
    t.add_argument("--location", default=None, type=lambda s: _parse_vec3(s, "location"))
    t.add_argument("--rotation", default=None, type=lambda s: _parse_vec3(s, "rotation"))
    t.add_argument("--scale", default=None, type=lambda s: _parse_vec3(s, "scale"))

    d = sub.add_parser("delete", help="delete a named object")
    d.add_argument("name")

    s = sub.add_parser("save", help="save the .blend to an absolute path")
    s.add_argument("path")

    r = sub.add_parser("render", help="deterministic Eevee still render")
    r.add_argument("output")
    r.add_argument("--engine", default="BLENDER_EEVEE")
    r.add_argument("--resolution", default=(256, 256), type=_parse_resolution)
    r.add_argument("--percentage", default=100, type=int)

    e = sub.add_parser("exec", help="run trusted bpy Python on Blender's main thread "
                                    "(requires allow_script_execution:true)")
    src = e.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", default=None,
                     help="read Python source from a file (preferred)")
    src.add_argument("--stdin", action="store_true",
                     help="read Python source from stdin")
    src.add_argument("--source", "-c", default=None,
                     help="small inline Python source (avoid giant shell-quoted strings)")
    e.add_argument("--label", default=None,
                   help="optional human-readable task label (<=256 chars)")
    e.add_argument("--timeout", default=None, type=float,
                   help="seconds to wait for a result (default: script bound)")

    sub.add_parser("gen-token", help="print a random high-entropy hex token")
    return p


COMMANDS = {
    "ping": cmd_ping,
    "scene": cmd_scene,
    "create": cmd_create,
    "transform": cmd_transform,
    "delete": cmd_delete,
    "save": cmd_save,
    "render": cmd_render,
    "exec": cmd_exec,
    "gen-token": cmd_gen_token,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "gen-token":
        return cmd_gen_token(None, args)
    client = _build_client(args)
    if not client.token:
        print("blenderctl: no token (--token, BLENDER_CONTROL_TOKEN, or --config)",
              file=sys.stderr)
        return 1
    try:
        return COMMANDS[args.command](client, args)
    except RpcError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": {"code": e.code, "message": e.message}}))
        else:
            print("blenderctl: RPC failed [%s] %s" % (e.code, e.message), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("blenderctl: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
