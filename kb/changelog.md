# Changelog — notable changes, newest first.

The ONLY place release history lives (don't duplicate it in other notes).
One line per change that future-you or the agent should know about.

## Unreleased
- **Startup port reclaim** — `utils/port_reclaim.py#reclaim_port` is called before
  binding `FLASK_PORT`/`WEBSOCKET_PORT`/`ZMQ_PORT`, so a leftover listener from a
  hard-killed run no longer crashes startup with `RuntimeError: WebSocket port 9999
  is already in use`. Only a provably-ours, provably-dead process is stopped; a
  foreign program or a live server is reported with its pid instead. See
  [[features/startup-port-reclaim]].
- **`.env` always read as UTF-8** — every `open`/`load_dotenv` of `.env` names
  `encoding="utf-8"` (the Windows cp1252 default raised `'charmap' codec can't
  decode byte 0x9d`), and `run_qwantonomous.bat` sets `PYTHONUTF8=1` before python
  starts. Same note.
- (what changed) - why it matters. See [[architecture]].

## (date) - first version
- Initial knowledge base scaffolded.
