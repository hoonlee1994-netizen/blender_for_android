# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Android local control plane (v1): localhost JSON-RPC-like bridge to bpy.

Agent -> MCP/CLI -> TCP 127.0.0.1 -> socket thread -> queue ->
bpy.app.timers callback (Blender MAIN THREAD) -> bpy -> structured result.

Critical invariant: no bpy call ever runs on the socket/network thread.
Network I/O lives on background threads; every handler below marked
"MAIN THREAD ONLY" runs inside the bpy.app.timers callback.

Activation: the bridge stays disabled unless an explicit local control
config file enables it. Host tooling creates the file before launch:

    /storage/emulated/0/Download/blender-control.json

    {"enabled": true, "token": "<random high-entropy secret>", "port": 17878}

Absent / disabled / malformed config -> no server, with a clear log line.
The token is required on every request and is never logged.
"""

import hashlib
import hmac
import json
import math
import os
import queue
import socket
import sys
import threading
import traceback

__all__ = (
    "register",
    "unregister",
)

TAG = "[BlenderControl]"

PROTOCOL_VERSION = "1"
DEFAULT_PORT = 17878
BIND_HOST = "127.0.0.1"

CONFIG_ENV_OVERRIDE = "BLENDER_CONTROL_CONFIG"
CONFIG_DEFAULT_PATH = "/storage/emulated/0/Download/blender-control.json"
DEBUG_ENV = "BLENDER_CONTROL_DEBUG"

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
QUEUE_MAXSIZE = 32
MAX_DRAIN_PER_TICK = 4
TIMER_INTERVAL = 0.05
ACCEPT_POLL_TIMEOUT = 0.5
SOCKET_RECV_TIMEOUT = 60.0
DEFAULT_WAIT_TIMEOUT = 20.0
RENDER_WAIT_TIMEOUT = 180.0
MIN_TOKEN_LENGTH = 16

ALLOWED_CREATE_TYPES = ("CUBE", "UV_SPHERE")
ALLOWED_RENDER_ENGINES = ("BLENDER_EEVEE",)
INSPECT_OBJECT_CAP = 200


def _log(msg):
    print("%s %s" % (TAG, msg))


def _debug_enabled():
    return os.environ.get(DEBUG_ENV, "") == "1"


# ---------------------------------------------------------------------------
# Activation config
# ---------------------------------------------------------------------------

def _config_path():
    override = os.environ.get(CONFIG_ENV_OVERRIDE, "")
    if override:
        return override
    return CONFIG_DEFAULT_PATH


def _load_config():
    """Return (enabled_config_dict_or_None, status_string)."""
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read(MAX_REQUEST_BYTES + 1)
    except FileNotFoundError:
        return None, "disabled (no config at %s)" % path
    except OSError as e:
        _log("config unreadable at %s: %s; bridge disabled" % (path, e))
        return None, "disabled (unreadable config)"
    if len(raw) > MAX_REQUEST_BYTES:
        _log("config oversize at %s; bridge disabled" % path)
        return None, "disabled (oversize config)"
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        _log("malformed config at %s (%s); bridge disabled" % (path, e))
        return None, "disabled (malformed config)"
    if not isinstance(data, dict) or data.get("enabled") is not True:
        return None, "disabled (config not enabled at %s)" % path
    token = data.get("token", "")
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        _log("config at %s has missing/weak token; bridge disabled" % path)
        return None, "disabled (bad token)"
    port = data.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or not (1024 <= port <= 65535):
        _log("config at %s has invalid port; bridge disabled" % path)
        return None, "disabled (bad port)"
    return {"token": token, "port": port, "path": path}, "enabled"


# ---------------------------------------------------------------------------
# Request plumbing (socket thread side)
# ---------------------------------------------------------------------------

class _Request:
    __slots__ = ("req_id", "method", "params", "timeout", "event", "response")

    def __init__(self, req_id, method, params, timeout):
        self.req_id = req_id
        self.method = method
        self.params = params
        self.timeout = timeout
        self.event = threading.Event()
        self.response = None


def _fail(req_id, code, message):
    err = {"code": code, "message": message}
    if _debug_enabled() and code == "INTERNAL":
        err["traceback"] = traceback.format_exc()
    return {"id": req_id, "ok": False, "error": err}


def _ok(req_id, result):
    return {"id": req_id, "ok": True, "result": result}


# ---------------------------------------------------------------------------
# bpy handlers -- MAIN THREAD ONLY (called from the timers callback)
# ---------------------------------------------------------------------------

def _transform_of(obj):
    return {
        "location": [float(v) for v in obj.location],
        "rotation": [float(v) for v in obj.rotation_euler],
        "scale": [float(v) for v in obj.scale],
    }


def _check_vec3(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None, "%s must be a list of 3 numbers" % field
    try:
        return [float(v) for v in value], None
    except (TypeError, ValueError):
        return None, "%s must be a list of 3 numbers" % field


def _get_bpy():
    import bpy  # noqa: WPS433 -- Blender runtime import, main thread only
    return bpy


def h_ping(params, ctx):
    import bpy  # noqa -- main thread only
    main_ident_ok = threading.get_ident() == ctx["main_ident"]
    try:
        bl_version = ".".join(str(x) for x in bpy.app.version)
        bl_version_str = bpy.app.version_string
    except Exception:
        bl_version = "unknown"
        bl_version_str = "unknown"
    return {
        "protocol": PROTOCOL_VERSION,
        "blender_version": bl_version,
        "blender_version_string": bl_version_str,
        "platform": sys.platform,
        "android_api": getattr(sys, "getandroidapilevel", lambda: None)(),
        "pid": os.getpid(),
        "main_thread": main_ident_ok,
    }


def h_scene_inspect(params, ctx):
    bpy = _get_bpy()
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    active = view_layer.objects.active
    objs = list(scene.objects)
    total = len(objs)
    items = []
    for obj in objs[:INSPECT_OBJECT_CAP]:
        try:
            items.append({
                "name": obj.name,
                "type": obj.type,
                **_transform_of(obj),
            })
        except Exception:
            continue
    return {
        "scene": scene.name,
        "engine": scene.render.engine,
        "active_object": active.name if active is not None else None,
        "selected": sorted(o.name for o in view_layer.objects if getattr(o, "select_get", lambda: False)()),
        "object_count": total,
        "truncated": total > INSPECT_OBJECT_CAP,
        "objects": items,
    }


def _normalize_create_type(value):
    if not isinstance(value, str):
        return None
    return value.upper().replace("-", "_")


def _validate_new_name(name):
    if not isinstance(name, str) or not (1 <= len(name) <= 64):
        return "name must be a 1..64 character string"
    for ch in name:
        if not (ch.isalnum() or ch in "_.-"):
            return "name may only contain letters, digits, '_' '.' '-'"
    return None


def _make_cube_mesh(bpy, mesh):
    verts = [(-1, -1, -1), (-1, -1, 1), (-1, 1, -1), (-1, 1, 1),
             (1, -1, -1), (1, -1, 1), (1, 1, -1), (1, 1, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
             (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()


def _make_uv_sphere_mesh(bpy, mesh, segments=16, rings=8):
    verts = [(0.0, 0.0, 1.0)]
    for i in range(1, rings):
        phi = math.pi * i / rings
        z = math.cos(phi)
        r = math.sin(phi)
        for j in range(segments):
            theta = 2.0 * math.pi * j / segments
            verts.append((r * math.cos(theta), r * math.sin(theta), z))
    verts.append((0.0, 0.0, -1.0))
    faces = []
    for j in range(segments):
        faces.append((0, 1 + j, 1 + (j + 1) % segments))
    for i in range(rings - 2):
        base0 = 1 + i * segments
        base1 = 1 + (i + 1) * segments
        for j in range(segments):
            j1 = (j + 1) % segments
            faces.append((base0 + j, base1 + j, base1 + j1, base0 + j1))
    cap_base = 1 + (rings - 2) * segments
    bottom = len(verts) - 1
    for j in range(segments):
        faces.append((cap_base + j, cap_base + (j + 1) % segments, bottom))
    mesh.from_pydata(verts, [], faces)
    mesh.update()


def h_object_create(params, ctx):
    bpy = _get_bpy()
    if not isinstance(params, dict):
        return _handler_error("INVALID_PARAMS", "params must be an object")
    ctype = _normalize_create_type(params.get("type"))
    if ctype not in ALLOWED_CREATE_TYPES:
        return _handler_error(
            "INVALID_PARAMS",
            "type must be one of %s" % (list(ALLOWED_CREATE_TYPES),))
    name = params.get("name")
    if name is None:
        base = {"CUBE": "Cube", "UV_SPHERE": "Sphere"}[ctype]
        name = base
        i = 1
        while name in bpy.data.objects:
            i += 1
            name = "%s.%03d" % (base, i)
    else:
        err = _validate_new_name(name)
        if err:
            return _handler_error("INVALID_PARAMS", err)
        if name in bpy.data.objects:
            return _handler_error("OBJECT_EXISTS", "Object '%s' already exists" % name)
    mesh = bpy.data.meshes.new(name + "_mesh")
    try:
        if ctype == "CUBE":
            _make_cube_mesh(bpy, mesh)
        else:
            _make_uv_sphere_mesh(bpy, mesh)
    except Exception as e:
        bpy.data.meshes.remove(mesh)
        return _handler_error("INTERNAL", "mesh build failed: %s" % e)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    try:
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
    except Exception:
        pass
    terr = _apply_transform(obj, params)
    if terr:
        return terr
    return {"name": obj.name, **_transform_of(obj)}


def _apply_transform(obj, params):
    for field in ("location", "rotation", "scale"):
        if field in params and params[field] is not None:
            vec, err = _check_vec3(params[field], field)
            if err:
                return _handler_error("INVALID_PARAMS", err)
            if field == "location":
                obj.location = vec
            elif field == "rotation":
                try:
                    obj.rotation_euler = vec
                except Exception as e:
                    return _handler_error("INVALID_PARAMS", "rotation rejected: %s" % e)
            else:
                if any(v == 0.0 for v in vec):
                    return _handler_error("INVALID_PARAMS", "scale components must be non-zero")
                obj.scale = vec
    return None


def h_object_transform(params, ctx):
    bpy = _get_bpy()
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _handler_error("INVALID_PARAMS", "params.name (string) is required")
    obj = bpy.data.objects.get(params["name"])
    if obj is None:
        return _handler_error("OBJECT_NOT_FOUND", "Object '%s' does not exist" % params["name"])
    if not any(k in params for k in ("location", "rotation", "scale")):
        return _handler_error("INVALID_PARAMS", "nothing to change: pass location/rotation/scale")
    terr = _apply_transform(obj, params)
    if terr:
        return terr
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    return {"name": obj.name, **_transform_of(obj)}


def h_object_delete(params, ctx):
    bpy = _get_bpy()
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _handler_error("INVALID_PARAMS", "params.name (string) is required")
    obj = bpy.data.objects.get(params["name"])
    if obj is None:
        return _handler_error("OBJECT_NOT_FOUND", "Object '%s' does not exist" % params["name"])
    name = obj.name
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception as e:
        return _handler_error("INTERNAL", "delete failed: %s" % e)
    return {"deleted": name}


def h_scene_save(params, ctx):
    bpy = _get_bpy()
    if not isinstance(params, dict) or not isinstance(params.get("path"), str):
        return _handler_error("INVALID_PARAMS", "params.path (string) is required")
    path = params["path"]
    if not os.path.isabs(path):
        return _handler_error("INVALID_PATH", "path must be absolute: %r" % path)
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
    except OSError as e:
        return _handler_error("INVALID_PATH", "cannot create parent dir: %s" % e)
    try:
        bpy.ops.wm.save_as_mainfile(filepath=path)
    except Exception as e:
        return _handler_error("SAVE_FAILED", "save failed: %s" % e)
    actual = bpy.data.filepath or path
    return {"path": actual}


def h_render_still(params, ctx):
    bpy = _get_bpy()
    if not isinstance(params, dict) or not isinstance(params.get("output"), str):
        return _handler_error("INVALID_PARAMS", "params.output (string) is required")
    output = params["output"]
    if not os.path.isabs(output):
        return _handler_error("INVALID_PATH", "output must be absolute: %r" % output)
    engine = params.get("engine", "BLENDER_EEVEE")
    if engine not in ALLOWED_RENDER_ENGINES:
        return _handler_error(
            "INVALID_PARAMS",
            "engine must be one of %s" % (list(ALLOWED_RENDER_ENGINES),))
    try:
        res_x = int(params.get("resolution_x", 256))
        res_y = int(params.get("resolution_y", 256))
        pct = int(params.get("percentage", 100))
    except (TypeError, ValueError):
        return _handler_error("INVALID_PARAMS", "resolution_x/y and percentage must be integers")
    if not (8 <= res_x <= 2048 and 8 <= res_y <= 2048):
        return _handler_error("INVALID_PARAMS", "resolution_x/y must each be 8..2048")
    if not (1 <= pct <= 100):
        return _handler_error("INVALID_PARAMS", "percentage must be 1..100")
    parent = os.path.dirname(output)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
    except OSError as e:
        return _handler_error("INVALID_PATH", "cannot create parent dir: %s" % e)
    scene = bpy.context.scene
    try:
        scene.render.engine = engine
        scene.render.resolution_x = res_x
        scene.render.resolution_y = res_y
        scene.render.resolution_percentage = pct
        scene.render.filepath = output
        try:
            scene.render.image_settings.file_format = "PNG"
        except Exception:
            pass
        bpy.ops.render.render(write_still=True)
    except Exception as e:
        return _handler_error("RENDER_FAILED", "render failed: %s" % e)
    width = res_x * pct // 100
    height = res_y * pct // 100
    return {"engine": engine, "output": output, "width": width, "height": height}


def _handler_error(code, message):
    return {"__error__": {"code": code, "message": message}}


HANDLERS = {
    "ping": (h_ping, DEFAULT_WAIT_TIMEOUT),
    "scene.inspect": (h_scene_inspect, DEFAULT_WAIT_TIMEOUT),
    "object.create": (h_object_create, DEFAULT_WAIT_TIMEOUT),
    "object.transform": (h_object_transform, DEFAULT_WAIT_TIMEOUT),
    "object.delete": (h_object_delete, DEFAULT_WAIT_TIMEOUT),
    "scene.save": (h_scene_save, DEFAULT_WAIT_TIMEOUT),
    "render.still": (h_render_still, RENDER_WAIT_TIMEOUT),
}

FORBIDDEN_PREFIXES = ("python.", "shell.", "subprocess.", "os.", "sys.", "exec", "eval")


# ---------------------------------------------------------------------------
# Bridge lifetime
# ---------------------------------------------------------------------------

class _Bridge:
    def __init__(self, token, port):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        _log("initializing (token sha256 prefix %s...)" % token_hash)
        self._token = token
        self._port = port
        self._queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._stop = threading.Event()
        self._main_ident = threading.get_ident()
        self._server_thread = None
        self._listen_sock = None
        self._timer_active = False

    # -- socket side ------------------------------------------------------
    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((BIND_HOST, self._port))
            sock.listen(4)
            sock.settimeout(ACCEPT_POLL_TIMEOUT)
        except OSError as e:
            _log("bind %s:%d failed (%s); bridge NOT started" % (BIND_HOST, self._port, e))
            sock.close()
            return False
        self._listen_sock = sock
        self._register_timer()
        t = threading.Thread(target=self._serve, name="BlenderControlServer", daemon=True)
        t.start()
        self._server_thread = t
        _log("listening on %s:%d (protocol v%s)" % (BIND_HOST, self._port, PROTOCOL_VERSION))
        return True

    def stop(self):
        self._stop.set()
        try:
            if self._listen_sock is not None:
                self._listen_sock.close()
        except OSError:
            pass
        self._unregister_timer()
        # Drain the queue so any late timer tick exits quietly.
        try:
            while True:
                req = self._queue.get_nowait()
                req.response = _fail(req.req_id, "INTERNAL", "bridge shutting down")
                req.event.set()
        except queue.Empty:
            pass
        _log("stopped")

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_conn, args=(conn,),
                                 name="BlenderControlConn", daemon=True)
            t.start()

    def _handle_conn(self, conn):
        try:
            conn.settimeout(SOCKET_RECV_TIMEOUT)
            buf = b""
            with conn:
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        break
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if len(line) > MAX_REQUEST_BYTES:
                            self._send(conn, _fail(None, "INVALID_REQUEST", "request too large"))
                            buf = b""
                            break
                        self._dispatch_line(conn, line)
                        if len(buf) > MAX_REQUEST_BYTES:
                            self._send(conn, _fail(None, "INVALID_REQUEST", "request too large"))
                            buf = b""
                            break
                    if len(buf) > MAX_REQUEST_BYTES:
                        self._send(conn, _fail(None, "INVALID_REQUEST", "request too large"))
                        break
        except Exception as e:  # never let one connection kill the server
            _log("connection handler error (%s); continuing" % type(e).__name__)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, conn, payload):
        try:
            data = (json.dumps(payload) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            data = b'{"id": null, "ok": false, "error": {"code": "INTERNAL", "message": "unserializable result"}}\n'
        if len(data) > MAX_RESPONSE_BYTES:
            data = (json.dumps(_fail(payload.get("id"), "INTERNAL", "response too large")) + "\n").encode()
        try:
            conn.sendall(data)
        except OSError:
            pass

    def _dispatch_line(self, conn, line):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self._send(conn, _fail(None, "MALFORMED_JSON", "request is not valid UTF-8"))
            return
        if not text.strip():
            return
        try:
            msg = json.loads(text)
        except ValueError:
            self._send(conn, _fail(None, "MALFORMED_JSON", "request is not valid JSON"))
            return
        req_id = msg.get("id") if isinstance(msg, dict) else None
        if not isinstance(msg, dict):
            self._send(conn, _fail(req_id, "INVALID_REQUEST", "request must be a JSON object"))
            return
        method = msg.get("method")
        params = msg.get("params", {})
        if not isinstance(method, str) or not method:
            self._send(conn, _fail(req_id, "INVALID_REQUEST", "missing method"))
            return
        token = msg.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            _log("auth failure for method=%r id=%r" % (method, req_id))
            self._send(conn, _fail(req_id, "AUTH_FAILED", "invalid token"))
            return
        if method in ("python.eval", "python.exec") or method.startswith(FORBIDDEN_PREFIXES):
            self._send(conn, _fail(req_id, "FORBIDDEN", "method not allowed: %s" % method))
            return
        entry = HANDLERS.get(method)
        if entry is None:
            self._send(conn, _fail(req_id, "UNKNOWN_METHOD", "unknown method: %s" % method))
            return
        _handler, timeout = entry
        req = _Request(req_id, method, params if isinstance(params, dict) else {}, timeout)
        try:
            self._queue.put_nowait(req)
        except queue.Full:
            _log("busy (queue full) method=%s id=%r" % (method, req_id))
            self._send(conn, _fail(req_id, "BUSY", "server busy, retry later"))
            return
        _log("recv method=%s id=%r" % (method, req_id))
        if not req.event.wait(timeout):
            _log("timeout method=%s id=%r" % (method, req_id))
            self._send(conn, _fail(req_id, "TIMEOUT", "request timed out"))
            return
        resp = req.response if req.response is not None else _fail(req_id, "INTERNAL", "no response")
        if isinstance(resp, dict) and resp.get("ok") is True:
            _log("done method=%s id=%r" % (method, req_id))
        else:
            code = "UNKNOWN"
            try:
                code = resp["error"]["code"]
            except (KeyError, TypeError):
                pass
            _log("fail method=%s id=%r code=%s" % (method, req_id, code))
        self._send(conn, resp)

    # -- main-thread side ---------------------------------------------------
    def _register_timer(self):
        try:
            import bpy  # noqa -- main thread only
        except ImportError:
            _log("bpy unavailable; timer not registered (bridge socket-only)")
            return
        try:
            if bpy.app.timers.is_registered(self._pump):
                return
            bpy.app.timers.register(self._pump, first_interval=0.2, persistent=True)
            self._timer_active = True
        except Exception as e:
            _log("timer registration failed (%s)" % e)

    def _unregister_timer(self):
        if not self._timer_active:
            return
        self._timer_active = False
        try:
            import bpy  # noqa -- main thread only
            if bpy.app.timers.is_registered(self._pump):
                bpy.app.timers.unregister(self._pump)
        except Exception:
            pass

    def _pump(self):
        """Runs on Blender's main thread via bpy.app.timers. Drains a bounded
        number of queued requests per tick so the UI stays responsive."""
        if self._stop.is_set():
            return None
        if threading.get_ident() != self._main_ident:
            _log("WARNING: timer callback not on registration thread; bpy calls skipped this tick")
            return TIMER_INTERVAL
        for _ in range(MAX_DRAIN_PER_TICK):
            try:
                req = self._queue.get_nowait()
            except queue.Empty:
                break
            if req.event.is_set():
                continue  # waiter already timed out; drop
            try:
                _handler, _timeout = HANDLERS[req.method]
                ctx = {"main_ident": self._main_ident}
                result = _handler(req.params, ctx)
                if isinstance(result, dict) and "__error__" in result:
                    info = result["__error__"]
                    req.response = _fail(req.req_id, info.get("code", "INTERNAL"),
                                         info.get("message", "handler failed"))
                else:
                    req.response = _ok(req.req_id, result)
            except Exception as e:
                _log("handler exception method=%s (%s)" % (req.method, type(e).__name__))
                if _debug_enabled():
                    traceback.print_exc()
                req.response = _fail(req.req_id, "INTERNAL", "handler failed: %s" % type(e).__name__)
            finally:
                req.event.set()
        return TIMER_INTERVAL


_BRIDGE = None


def register():
    global _BRIDGE
    if _BRIDGE is not None:
        try:
            _BRIDGE.stop()
        except Exception:
            pass
        _BRIDGE = None
    config, status = _load_config()
    _log("bridge %s" % status)
    if config is None:
        return
    bridge = _Bridge(config["token"], config["port"])
    if bridge.start():
        _BRIDGE = bridge
        _log("bridge active; bpy executes on main thread via bpy.app.timers")
    else:
        _log("bridge failed to start; bpy untouched")


def unregister():
    global _BRIDGE
    if _BRIDGE is not None:
        try:
            _BRIDGE.stop()
        except Exception:
            pass
        _BRIDGE = None
