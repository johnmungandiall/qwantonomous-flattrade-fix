"""Reclaim a startup port from a dead or hung copy of THIS app.

OpenAlgo binds two fixed ports and cannot move them at runtime (SDK clients
look for exactly these): ``FLASK_PORT`` (5001 on this install) and
``WEBSOCKET_PORT`` (9999). When a previous run dies hard - or the WebSocket
child outlives its parent - the listener stays bound and the next start fails
with::

    RuntimeError: WebSocket port 9999 is already in use on 127.0.0.1
    OSError: [WinError 10048] ... only one usage of each socket address

This module decides, *before* binding, whether the process holding the port is
a stale leftover of this very project that nobody is using any more. Only then
is it stopped. The safety boundary is deliberately narrow:

* A process that does NOT belong to this project is never touched - its pid,
  name and a copy/paste-able command are reported instead.
* A process of this project that is still *serving* is never touched either:
  an HTTP port that answers requests, or a WebSocket listener with client
  connections attached, is a running server. Starting a second copy on the same
  port is not what the operator wants, so we say so and stop.
* Our own pid and its ancestors are never signalled.

Staleness, per purpose:

``purpose="http"``
    Stale when the process holds the port but answers no HTTP request on it -
    a hung/aborted Flask run. A live Flask answers instantly.

``purpose="websocket"``
    Stale when the listener has no live ``app.py`` ancestor, no client
    connection attached and answers no request - the classic orphaned
    ``python -m websocket_proxy.server`` child.

The caller decides what to do with a refused reclaim (raise, or print and
exit); this module only measures and, when it is safe, cleans up.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, Sequence

# Root of the checkout this module lives in (server/), used to decide whether a
# listener belongs to THIS project rather than some other python program.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Command-line fragments that only our own entry points contain. Matched as
# whole path tokens, never as substrings: "..._app.py" or "test_app.pyx" are
# different programs, and only a real "app.py"/"websocket_proxy.server"
# argument identifies this server.
_APP_MARKERS = (
    re.compile(r'(?<![\w.-])app\.py(?![\w-])'),
    re.compile(r'(?<![\w.-])websocket_proxy[\\/]server\.py(?![\w-])'),
    re.compile(r'(?<![\w.-])websocket_proxy\.server(?![\w-])'),
)

# Interpreter names that mean "this is a python process": python.exe,
# pythonw.exe, python3.exe, python3.13.exe, py.exe. A prefix test is not good
# enough - "powershell" also starts with "py".
_PYTHON_NAME = re.compile(r"^(?:py|pyw|python[0-9.]*|pythonw[0-9.]*)$")

# Addresses that mean "the interface probe" rather than a real peer address.
_WILDCARD_HOSTS = {"", "0.0.0.0", "::", "[::]", "*"}

# How often a silent port is re-probed before it is called dead, and the gap in
# seconds between attempts. A live-but-busy server can miss one probe; it does
# not miss three.
_PROBE_ATTEMPTS = 3
_PROBE_GAP = 1.2


@dataclass(frozen=True)
class PortOwner:
    """A process listening on the port we want to bind."""

    pid: int
    name: str
    cmdline: str
    cwd: str
    is_ours: bool


@dataclass(frozen=True)
class ReclaimResult:
    """Outcome of :func:`reclaim_port`."""

    host: str
    port: int
    reclaimed: bool
    killed: tuple[int, ...]
    owners: tuple[PortOwner, ...]
    reason: str

    @property
    def held_by_ours(self) -> bool:
        return any(o.is_ours for o in self.owners)


def probe_host(host: str) -> str:
    """Return an address that can actually be connected to for ``host``.

    ``0.0.0.0``/``::`` are bind-only addresses; a probe must use loopback. A
    concrete address is returned unchanged so a server bound to a LAN/IP
    address is still probed where it really listens.
    """
    if host is None or str(host).strip().lower() in _WILDCARD_HOSTS:
        return "127.0.0.1"
    return str(host).strip()


def _logger():
    """Return the project logger, falling back to stdlib logging."""
    try:
        from utils.logging import get_logger

        return get_logger("port_reclaim")
    except Exception:  # pragma: no cover - only when the project logger is broken
        import logging

        return logging.getLogger("port_reclaim")


def _norm(text: str) -> str:
    """Normalise a path/command line for substring comparison."""
    return str(text).replace("/", "\\").lower()


def _is_python_process(name: str) -> bool:
    """True for a python interpreter's image name (never for "powershell")."""
    base = (name or "").strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return bool(_PYTHON_NAME.match(base))


def _has_app_marker(cmdline: str) -> bool:
    """True when the command line runs one of this app's entry points."""
    text = str(cmdline or "").replace("/", "\\")
    return any(marker.search(text) for marker in _APP_MARKERS)


def _current_pids() -> set[int]:
    """Our own pid plus every ancestor - these are never signalled."""
    pids = {os.getpid()}
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        for _ in range(8):
            parent = proc.parent()
            if parent is None:
                break
            pids.add(parent.pid)
            proc = parent
    except Exception:
        pass
    return pids


def _describe(pid: int, root: Optional[str] = None) -> Optional[PortOwner]:
    """Build a :class:`PortOwner` for ``pid``; ``None`` when it is gone."""
    try:
        import psutil

        proc = psutil.Process(pid)
        name = proc.name()
        try:
            cmdline = " ".join(proc.cmdline())
        except (psutil.AccessDenied, psutil.ZombieProcess):
            cmdline = ""
        try:
            cwd = proc.cwd()
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            cwd = ""
    except Exception:
        return None
    owner = PortOwner(pid=pid, name=name, cmdline=cmdline, cwd=cwd, is_ours=False)
    return PortOwner(
        pid=owner.pid,
        name=owner.name,
        cmdline=owner.cmdline,
        cwd=owner.cwd,
        is_ours=is_our_app_process(owner, root),
    )


def is_our_app_process(owner: PortOwner, root: Optional[str] = None) -> bool:
    """True when ``owner`` is a python process running THIS checkout.

    Both halves are required: a python interpreter (so we never mistake a
    browser or a service for our own code) *and* an entry point of this app
    (``app.py`` / ``websocket_proxy.server``) together with evidence that the
    process works in this project root - its command line or its cwd.
    """
    if not _is_python_process(owner.name):
        return False

    root_norm = _norm(root or PROJECT_ROOT).rstrip("\\")
    cmd = _norm(owner.cmdline)
    cwd = _norm(owner.cwd).rstrip("\\")

    if not _has_app_marker(owner.cmdline):
        return False

    in_root = bool(root_norm) and (root_norm in cmd or cwd == root_norm or cwd.startswith(root_norm + "\\"))
    return in_root


def has_live_ancestor_app(pid: int, root: Optional[str] = None, max_depth: int = 6) -> bool:
    """True when ``pid`` still has a running ``app.py`` of this project above it.

    This is what separates a healthy ``websocket_proxy.server`` child (started by
    the app, still doing its job) from an orphan left behind when the parent died.
    """
    try:
        import psutil

        proc = psutil.Process(pid)
    except Exception:
        return False

    for _ in range(max_depth):
        try:
            parent = proc.parent()
        except Exception:
            return False
        if parent is None:
            return False
        described = _describe(parent.pid, root)
        if described is not None and _has_app_marker(described.cmdline) and described.is_ours:
            return True
        proc = parent
    return False


def _address_matches(bind_host: str, listen_ip: str) -> bool:
    """Does a listener on ``listen_ip`` conflict with a bind on ``bind_host``?"""
    listen = str(listen_ip or "").lower()
    host = str(bind_host or "").lower()
    if listen in {"0.0.0.0", "::", "*", ""} or host in {"0.0.0.0", "::", "*", ""}:
        return True
    if listen in {"localhost", "::1", "127.0.0.1"} and host in {"localhost", "::1", "127.0.0.1"}:
        return True
    return listen == host


def listening_pids(host: str, port: int) -> tuple[int, ...]:
    """Pids with a TCP LISTEN socket on ``port`` that conflicts with ``host``."""
    try:
        import psutil
    except Exception:
        return ()

    pids: list[int] = []
    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception:
        return ()

    for conn in conns:
        if not conn.laddr or getattr(conn.laddr, "port", None) != int(port):
            continue
        if getattr(conn, "status", "") not in {"LISTEN", "LISTENING", "listen", "listening"}:
            continue
        if not _address_matches(host, getattr(conn.laddr, "ip", "")):
            continue
        if conn.pid and conn.pid not in pids:
            pids.append(conn.pid)
    return tuple(pids)


def active_client_count(host: str, port: int) -> Optional[int]:
    """Number of established connections on ``port``; ``None`` when unknown."""
    try:
        import psutil

        conns = psutil.net_connections(kind="tcp")
    except Exception:
        return None

    count = 0
    for conn in conns:
        if getattr(conn, "status", "") not in {"ESTABLISHED", "SYN_RECV", "SYN_SENT"}:
            continue
        if conn.laddr and getattr(conn.laddr, "port", None) == int(port):
            count += 1
        elif conn.raddr and getattr(conn.raddr, "port", None) == int(port):
            count += 1
    return count


def answers_request(host: str, port: int, timeout: float = 2.0) -> bool:
    """True when something on ``port`` replies to a plain HTTP GET.

    Any reply byte counts - a Flask app answers 200/302, the ``websockets``
    listener answers 400 to a non-upgrade request. Silence means the holder is
    not serving at all. The request is a read-only ``GET /``.
    """
    addresses = []
    primary = probe_host(host)
    addresses.append(primary)
    if primary != "127.0.0.1":
        addresses.append("127.0.0.1")

    for address in addresses:
        try:
            with socket.create_connection((address, int(port)), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(f"GET / HTTP/1.0\r\nHost: {address}\r\n\r\n".encode("ascii", "ignore"))
                if sock.recv(1):
                    return True
        except OSError:
            continue
    return False


def can_bind(host: str, port: int) -> bool:
    """True when this process could really bind ``host:port`` right now.

    Windows needs care: ``SO_REUSEADDR`` there allows a bind to SUCCEED on an
    address another socket is already listening on, so a reuse-address probe
    reports a taken port as free. ``SO_EXCLUSIVEADDRUSE`` is the truthful
    answer on Windows; POSIX keeps ``SO_REUSEADDR`` so a socket lingering in
    TIME_WAIT is not mistaken for a live holder.
    """
    address = probe_host(host)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                except OSError:
                    pass
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((address, int(port)))
            return True
        except OSError:
            return False


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone. Avoids ``psutil.Process.wait`` on purpose.

    ``wait()`` takes the pidfd/select path, which is unsafe under an
    eventlet-monkey-patched process (the pattern already documented in
    ``blueprints/python_strategy.py``). Plain polling works everywhere.
    """
    try:
        import psutil
    except Exception:
        return True

    deadline = time.time() + max(0.2, timeout)
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            proc = psutil.Process(pid)
            if proc.status() in {psutil.STATUS_ZOMBIE, getattr(psutil, "STATUS_DEAD", "dead")}:
                return True
        except Exception:
            return True  # NoSuchProcess / AccessDenied on a dying pid
        time.sleep(0.1)
    return not psutil.pid_exists(pid)


def _terminate(pid: int, timeout: float) -> bool:
    """Stop ``pid`` (and its children on Windows). True when it is gone."""
    try:
        import psutil

        proc = psutil.Process(pid)
    except Exception:
        return True

    try:
        proc.terminate()
    except Exception:
        pass

    if _wait_gone(pid, timeout / 2):
        return True

    try:
        if sys.platform == "win32":
            # /T reaps the WebSocket child too; /F because Windows has no SIGTERM.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=max(5.0, timeout),
                check=False,
            )
        else:
            proc.kill()
    except Exception:
        pass

    return _wait_gone(pid, timeout / 2)


def _answers_after_retries(host: str, port: int) -> bool:
    """Ask the port up to ``_PROBE_ATTEMPTS`` times; True if it EVER answers.

    A busy or momentarily slow server can miss a single probe, so one silence
    must never be read as "dead" - that would let the reclaim stop a working
    trading server. Only consistent silence counts, and even then the caller
    also refuses when client connections are attached.
    """
    for attempt in range(_PROBE_ATTEMPTS):
        if answers_request(host, port):
            return True
        if attempt < _PROBE_ATTEMPTS - 1:
            time.sleep(_PROBE_GAP)
    return False


def _staleness(
    owner: PortOwner,
    purpose: str,
    host: str,
    port: int,
    root: Optional[str],
) -> tuple[bool, str]:
    """Decide whether ``owner`` is a stale leftover we may stop.

    Every path here is deliberately conservative. Stopping a listener that is
    actually serving costs the operator live trading, so the holder must show
    no sign of life on several independent signals before it is called stale:
    no live parent app, no attached clients, and no answer to repeated probes.
    """
    if purpose != "http":
        if has_live_ancestor_app(owner.pid, root):
            return False, "its app.py parent is still running"
        clients = active_client_count(host, port)
        if clients is None:
            return False, "could not determine whether clients are connected to it"
        if clients > 0:
            return False, f"{clients} client connection(s) are still attached to it"
        if _answers_after_retries(host, port):
            return False, "it still answers requests"
        return True, "orphaned WebSocket listener: no parent app, no clients, not answering"

    clients = active_client_count(host, port)
    if clients is None:
        return False, "could not determine whether clients are connected to it"
    if clients > 0:
        return False, f"{clients} client connection(s) are still attached to it"
    if _answers_after_retries(host, port):
        return False, "it answers HTTP requests, so a server is already running on it"
    return True, "it holds the port, has no clients, and does not answer requests"


def reclaim_port(
    host: str,
    port: int,
    *,
    purpose: str = "http",
    root: Optional[str] = None,
    logger=None,
    kill_timeout: float = 10.0,
    probe_timeout: float = 2.0,
) -> ReclaimResult:
    """Free ``host:port`` when its holder is a stale copy of this app.

    Args:
        host: Address the caller intends to bind.
        port: Port the caller intends to bind.
        purpose: ``"http"`` for the Flask port; ``"websocket"`` for the WebSocket
            port and ``"bus"`` for the ZeroMQ port (both are served by a child
            that has its own ``app.py`` parent, so both require the
            parent/clients/probe staleness test).
        root: Project root used to recognise our own processes (defaults to
            this checkout).
        logger: Logger to report on; the project logger is used otherwise.
        kill_timeout: Seconds to wait for a stale process to disappear.
        probe_timeout: Seconds for the single liveness probe.

    Returns:
        ReclaimResult: ``reclaimed`` is True when the port is free for the
        caller. ``reason`` is a plain sentence explaining the outcome either
        way.
    """
    log = logger or _logger()
    project_root = root or PROJECT_ROOT

    pids = listening_pids(host, port)
    if not pids:
        if can_bind(host, port):
            return ReclaimResult(host, port, True, (), (), f"port {port} is already free")
        return ReclaimResult(
            host,
            port,
            False,
            (),
            (),
            f"port {port} is blocked but the owning process could not be identified "
            f"(run netstat -ano | findstr :{port} to see it)",
        )

    owners = tuple(o for o in (_describe(pid, project_root) for pid in pids) if o is not None)
    if not owners:
        return ReclaimResult(
            host,
            port,
            False,
            (),
            (),
            f"port {port} is held by a process that has already exited or is not readable",
        )

    protected = _current_pids()
    foreign = [o for o in owners if not o.is_ours and o.pid not in protected]
    if foreign:
        detail = ", ".join(f"pid {o.pid} ({o.name or 'unknown'})" for o in foreign)
        return ReclaimResult(
            host,
            port,
            False,
            (),
            owners,
            f"port {port} is held by another program: {detail}",
        )

    blocked: list[str] = []
    doomed: list[PortOwner] = []
    for owner in owners:
        if owner.pid in protected:
            blocked.append(f"pid {owner.pid} is this process or its parent")
            continue
        stale, why = _staleness(owner, purpose, host, port, project_root)
        if stale:
            doomed.append(owner)
        else:
            blocked.append(f"pid {owner.pid}: {why}")

    if blocked:
        return ReclaimResult(host, port, False, (), owners, "; ".join(blocked))

    killed: list[int] = []
    for owner in doomed:
        log.warning(
            f"Port {port} was still held by a leftover process of this app "
            f"(pid {owner.pid}); stopping it before binding."
        )
        if _terminate(owner.pid, kill_timeout):
            killed.append(owner.pid)
        else:
            return ReclaimResult(
                host,
                port,
                False,
                tuple(killed),
                owners,
                f"pid {owner.pid} did not exit after {kill_timeout:.0f}s",
            )

    # Give the OS a moment to release the socket, then confirm we can bind.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if can_bind(host, port):
            return ReclaimResult(
                host,
                port,
                True,
                tuple(killed),
                owners,
                f"stopped leftover pid(s) {', '.join(str(p) for p in killed)} and freed port {port}",
            )
        time.sleep(0.1)

    return ReclaimResult(
        host,
        port,
        False,
        tuple(killed),
        owners,
        f"stopped pid(s) {', '.join(str(p) for p in killed)} but port {port} is still bound",
    )


def manual_release_hint(port: int) -> str:
    """Copy/paste-able commands to free ``port`` by hand, per platform."""
    if sys.platform == "win32":
        return (
            f"     netstat -ano | findstr :{port}\n"
            f"     taskkill /PID <pid-from-the-list> /F"
        )
    return f"     lsof -ti:{port} | xargs kill -9"


def describe_owners(owners: Sequence[PortOwner]) -> str:
    """One readable line per listener, for an error message."""
    if not owners:
        return "no listener identified"
    return "; ".join(f"pid {o.pid} ({o.name or 'unknown'})" for o in owners)
