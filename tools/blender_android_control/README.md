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
  one request per line, 256 KiB request cap, bounded queue (32) and timeouts.
* Requires the token from the activation config on **every** request
  (`hmac.compare_digest`). The token never appears in logs.
* Network I/O runs on background threads. **All bpy execution is marshalled
  to Blender's main thread** through a `bpy.app.timers` callback that drains
  at most 4 queued requests per tick. The timer callback asserts it runs on
  the registration thread; `ping` reports `main_thread`. Script execution
  uses the exact same queue/timers path — no second interpreter, no
  multiprocessing, no second Blender process, no Java-side Python.
* Malformed JSON returns a structured failure; one failed request never kills
  later requests; no traceback leaves the bridge unless `BLENDER_CONTROL_DEBUG=1`.
* Only the v1 API exists: `ping`, `scene.inspect`, `object.create` (CUBE,
  UV_SPHERE only), `object.transform`, `object.delete`, `scene.save`,
  `render.still` (BLENDER_EEVEE only — the sole engine in this checkout's
  RNA), plus the opt-in `script.execute` below. `python.eval` / `python.exec`
  (and any `python.*`, `shell.*`, `subprocess.*`, `os.*`, `sys.*`, `exec`,
  `eval` method) is rejected with `FORBIDDEN`. There is no generic
  shell/subprocess RPC method.
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

## Trusted script execution (`script.execute`, BPY-EXEC v1)

The seven semantic tools stay the preferred interface for common
deterministic operations. One explicit broad path covers the long tail of
Blender functionality:

```json
{"enabled": true, "token": "<64-hex-chars>", "port": 17878,
 "allow_script_execution": true}
```

* absent / `false` / malformed flag -> script execution disabled;
  `script.execute` answers deterministic `FORBIDDEN` while the semantic
  tools keep working. No second token or port.
* Request: `{"source": "<python>", "label": "optional <=256 chars"}`.
  Source bound: 256 KiB UTF-8. Each request gets a **fresh** execution
  namespace (no persistent globals); ordinary `import bpy`, `import math`,
  `from mathutils import Vector` work in the bundled interpreter.
* Response: `{"result": ..., "stdout": "...", "duration_ms": N,
  "source_sha256": "..."}`. A script may assign a top-level `result`
  holding JSON-serializable data; missing `result` -> `null`.
  Non-serializable `result` (e.g. a Blender object) is a structured
  `RESULT_NOT_SERIALIZABLE` error — never stringified. Captured
  stdout/stderr is bounded (16 KiB + truncation marker). The full source
  is never returned.
* Errors are deterministic: `FORBIDDEN`, `INVALID_PARAMS`,
  `SOURCE_TOO_LARGE`, `EXECUTION_ERROR` (type + concise message, plus a
  bounded traceback only when `BLENDER_CONTROL_DEBUG=1`), `RESULT_NOT_SERIALIZABLE`,
  `BUSY`, and the existing transport/timeout errors. The token never leaks.
* Long runs: the server waits up to 300 s per script; the shared client
  waits up to 330 s by default (pass an explicit shorter timeout to stop
  waiting earlier). A wait timeout means the **caller stops waiting, not
  cancellation**: code already on Blender's main thread cannot be safely
  interrupted and may run to completion. No rollback, no job scheduler.
* Logging is bounded metadata only (request id, label, source SHA-256,
  byte count, duration, outcome class) — never token, source, stdout, or
  file contents.

TRUST MODEL: «Enabling script execution grants the authenticated local
controller trusted Python execution inside Blender's process, with the
same operating-system access available to Blender's bundled Python
runtime.» This is not a sandbox, and no `os`/`subprocess`/filesystem/
network pseudo-restriction is attempted. The boundary is: localhost bind
only, mandatory high-entropy token, OFF by default with explicit opt-in.

TRANSACTION MODEL: there is no implicit rollback — arbitrary bpy code may
partially mutate the scene before failing, and no `.blend` is auto-saved
around scripts. For risky work the agent should save/checkpoint first
(`scene.save`), work on duplicated objects/collections, and save final
output only after validation.

CAPABILITY NOTE: broad *Python-API* access, not every GUI behavior. Prefer
Blender data APIs over UI-context-sensitive `bpy.ops`; some operators need
an Area/Region/modal context that background execution cannot provide. No
GUI automation is used as a fallback.

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

Trusted execution (requires `allow_script_execution:true` Blender-side):

```sh
python3 tools/blender_android_control/blenderctl.py exec --file task.py --label "bevel demo"
echo "result = {'scene': bpy.context.scene.name}" | \
    python3 tools/blender_android_control/blenderctl.py exec --stdin
```

Prefer `--file`/`--stdin` over giant shell-quoted `--source/-c` strings.

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

with `BLENDER_CONTROL_HOST/PORT/TOKEN` set. Tools (thin, over the RPC
client): `blender_ping`, `blender_scene_inspect`, `blender_object_create`,
`blender_object_transform`, `blender_object_delete`, `blender_scene_save`,
`blender_render_still`, plus exactly one broad tool `blender_bpy_execute`
(source + optional label) for trusted script execution. No `execute_shell`,
no generic host-command tool, no per-operator wrapper explosion.

## Tests

```sh
python3 -m unittest discover -s tools/blender_android_control/tests -v
```

Serialization, response parsing, auth rejection, malformed/truncated
responses, error-code propagation, CLI exit codes, MCP tool-surface shape,
plus the script-execution contract: opt-in gating, FORBIDDEN-by-default,
handler validation, size bounds, result/stdout/sha envelope,
non-serializable results, exceptions, fresh namespaces, client transport,
and CLI exec plumbing.
Physical-device evidence (Gate A/B transcripts) is the authority for the
bridge itself; bpy is not mocked.
