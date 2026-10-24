# Startup port reclaim + .env UTF-8 loading

How the server survives a hard restart: a leftover listener on 5001/9999/9555 no
longer crashes startup, and `.env` is no longer read with the Windows locale codec.

## The two startup failures this covers

1. `RuntimeError: WebSocket port 9999 is already in use on 127.0.0.1` — the fixed
   SDK/HTTP ports cannot be moved at runtime, so a stale listener kills startup
   unless a human finds the pid.
2. `Warning: Could not read .env file: 'charmap' codec can't decode byte 0x9d` — on
   Windows a bare `open()` uses cp1252, which cannot decode UTF-8 punctuation.

## Port reclaim — `utils/port_reclaim.py#reclaim_port`

Called BEFORE binding, from three places:

- `websocket_proxy/server.py#WebSocketProxy.__init__` — `purpose="websocket"` for
  `WEBSOCKET_PORT` (9999 here), and `purpose="bus"` for `ZMQ_PORT` (9555 here).
- `app.py` `__main__` — `purpose="http"` for `FLASK_PORT` (5001 here), before
  `socketio.run`.

The decision is `utils/port_reclaim.py#_staleness`. A holder is stopped ONLY when
every signal says it is dead:

| Check | Function | Meaning when it fails |
|---|---|---|
| is it this app? | `#is_our_app_process` | another program — reported, never killed |
| live `app.py` parent? | `#has_live_ancestor_app` | a serving WS child — spared |
| clients attached? | `#active_client_count` | in use — spared |
| answers a request? | `#answers_request` via `#_answers_after_retries` | serving — spared |
| our own pid/ancestor? | `#_current_pids` | never signalled |

Only after ALL of those come back dead does `#_terminate` run (`terminate`, then
`taskkill /F /T` on Windows). A refusal raises the same `RuntimeError` shape as
before, now with the real reason plus `#manual_release_hint` commands.

Two Windows traps this module had to get right:

- **`#can_bind` must use `SO_EXCLUSIVEADDRUSE`, not `SO_REUSEADDR`.** Verified on
  this machine: with `SO_REUSEADDR` a bind SUCCEEDS on a port that is already
  listening, so a reuse-address probe reports a taken port as free.
- **`#_is_python_process` must match an interpreter name exactly**
  (`^(py|pyw|python[0-9.]*|pythonw[0-9.]*)$`). A `startswith(("python", "py"))`
  test classifies `powershell.exe` as python.

The venv's `python.exe` is a redirector that STAYS ALIVE as the child's parent, so
tests that need a true orphan must spawn through `sys._base_executable` and let the
midwife exit (`test/test_startup_port_and_env_encoding.py`).

## .env UTF-8 — every read names the codec

`python-dotenv` already defaults to `utf-8`; the warning came from the bare `open()`
calls in `utils/env_check.py#check_env_version_compatibility`. All sites now pass
`encoding="utf-8"` explicitly:

- `utils/env_check.py#check_env_version_compatibility` (both `.env` and `.sample.env` opens)
- `utils/env_check.py#load_and_check_env_variables` (`load_dotenv`)
- `utils/config.py`, `utils/logging.py` (module-level loads)
- `websocket_proxy/app_integration.py#start_websocket_server`, `websocket_proxy/server.py#main` (`load_dotenv`)

`run_qwantonomous.bat` sets `set "PYTHONUTF8=1"` before the interpreter starts, so any
code path not yet audited also defaults to UTF-8.

## Verify

    .\\.venv\\Scripts\\python.exe -m pytest test\\test_startup_port_and_env_encoding.py -q

23 checks: address handling, owner discovery, "is this ours", foreign-never-killed,
own-pid-never-killed, real stale HTTP listener stopped, real orphaned WS listener
stopped, live-parent child spared, every `.env` reader declares utf-8, launcher sets
PYTHONUTF8 before python.

Related: [[gotchas]].
