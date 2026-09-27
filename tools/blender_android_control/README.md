# Blender Android control plane (v1)

Minimal but real programmatic control plane for Blender on Android:

```
Agent -> blenderctl CLI / MCP server -> shared RPC client (client.py)
  -> TCP 127.0.0.1:<port> -> bl_android_control.py (Blender startup script)
  -> background socket thread -> request queue
  -> bpy.app.timers callback on Blender's MAIN THREAD -> bpy
```

ADB is support infrastructure only (install/launch, port-forward, file
retrieval, logcat). No GUI automation.

## Layout

| Path | Role |
| --- | --- |
| `scripts/startup/bl_android_control.py` | Blender-side bridge (ships inside the APK via `package.sh`, which copies the whole `scripts/` tree). No native code. |
| `tools/blender_android_control/client.py` | Shared host-side RPC client (single transport implementation). |
| `tools/blender_android_control/blenderctl.py` | CLI, Gate A. |
| `tools/blender_android_control/mcp_server.py` | Thin MCP adapter over `client.py`, Gate B. Host-side only. |
| `tools/blender_android_control/tests/` | Stdlib `unittest` suite. |

## Blender-side bridge

* Binds **only** `127.0.0.1` (never `0.0.0.0`), newline-delimited UTF-8 JSON,
  one request per line, 64 KiB request cap, bounded queue (32) and timeouts.
* Requires the token from the activation config on **every** request
  (`hmac.compare_digest`). The token never appears in logs.
* Network I/O runs on background threads. **All bpy execution is marshalled
  to Blender's main thread** through a `bpy.app.timers` callback that drains
  at most 4 queued requests per tick. The timer callback asserts it runs on
  the registration thread; `ping` reports `main_thread`.
* Malformed JSON returns a structured failure; one failed request never kills
  later requests; no traceback leaves the bridge unless `BLENDER_CONTROL_DEBUG=1`.
* No arbitrary code execution: `python.eval` / `python.exec` (and any
  `python.*`, `shell.*`, `subprocess.*`, `os.*`, `sys.*`, `exec`, `eval`
  method) is rejected with `FORBIDDEN`. Only the semantic v1 API exists:
  `ping`, `scene.inspect`, `object.create` (CUBE, UV_SPHERE only),
  `object.transform`, `object.delete`, `scene.save`, `render.still`
  (BLENDER_EEVEE only — the sole engine in this checkout's RNA).
* Reload-safe: `register()` stops any previous instance first; shutdown never
  leaves duplicate server threads or timers.
* Logs under the `[BlenderControl]` tag (stdout/stderr already route to
  logcat under the Blender process).

## Activation / auth

Disabled unless explicitly enabled. Host tooling writes (before launch):

`/storage/emulated/0/Download/blender-control.json`

```json
{"enabled": true, "token": "<64-hex-chars>", "port": 17878}
```

* absent config -> no server; disabled flag -> no server;
  malformed config -> no server + clear log line; token shorter than
  16 chars -> no server.
* Generate the token with `blenderctl gen-token` (32 random bytes, hex).
  Never hard-code a secret into source or the APK.
* `BLENDER_CONTROL_CONFIG` env var overrides the config path (useful for
  desktop-Blender testing of the bridge).

## CLI (Gate A)

```sh
export BLENDER_CONTROL_TOKEN=<token>   # or --token / --config file
python3 tools/blender_android_control/blenderctl.py ping
python3 tools/blender_android_control/blenderctl.py scene
python3 tools/blender_android_control/blenderctl.py create cube --name Box
python3 tools/blender_android_control/blenderctl.py transform Box --location 1,2,3
python3 tools/blender_android_control/blenderctl.py delete Box
python3 tools/blender_android_control/blenderctl.py save /storage/emulated/0/Download/test.blend
python3 tools/blender_android_control/blenderctl.py render /storage/emulated/0/Download/test.png \
    --resolution 256x256 --percentage 50
```

Add `--json` for machine-readable output. Exit code is non-zero on any
RPC/application failure (2) or usage error (1).

## Connectivity

* Same-device agent / Termux: direct `127.0.0.1:17878`.
* External host: `adb forward tcp:<host-port> tcp:17878`, then
  `--host 127.0.0.1 --port <host-port>`. Blender never binds Wi-Fi/LAN.

## MCP server (Gate B, only after Gate A passes)

Official MCP SDK v1 API (`mcp<2`, FastMCP), host-side only — nothing MCP
enters the APK or Blender runtime:

```sh
uvx --python 3.12 --with "mcp<2" tools/blender_android_control/mcp_server.py
```

with `BLENDER_CONTROL_HOST/PORT/TOKEN` set. Tools (one-to-one over the RPC
client): `blender_ping`, `blender_scene_inspect`, `blender_object_create`,
`blender_object_transform`, `blender_object_delete`, `blender_scene_save`,
`blender_render_still`. No `execute_python`, no shell tool.

## Tests

```sh
python3 -m unittest discover -s tools/blender_android_control/tests -v
```

Serialization, response parsing, auth rejection, malformed/truncated
responses, error-code propagation, CLI exit codes, MCP tool-surface shape.
Physical-device evidence (Gate A/B transcripts) is the authority for the
bridge itself; bpy is not mocked.
