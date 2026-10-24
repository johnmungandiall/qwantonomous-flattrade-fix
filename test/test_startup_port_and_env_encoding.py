"""Tests for the startup port reclaim and the .env UTF-8 fixes.

Run with the project's own venv:

    .\\.venv\\Scripts\\python.exe -m pytest test\\test_startup_port_and_env_encoding.py -q

WHAT THIS PINS
  1. ``probe_host`` turns bind-only addresses (0.0.0.0 / ::) into something a
     probe can actually connect to, and leaves a real address alone.
  2. A listener is found by pid, and a FREE port is reported as free - the
     reclaim never invents an owner.
  3. ``is_our_app_process`` recognises only a python process running THIS
     checkout's entry point: a browser is not ours, and python running some
     other project's app.py is not ours either.
  4. A process holding the port that is this very process (or its parent) is
     NEVER killed - the reclaim refuses and says why.
  5. A real stale listener IS stopped and the port becomes bindable. This runs a
     genuine child process on a throwaway port, so the kill path is exercised
     for real, not mocked.
  6. Every .env read in the startup path passes encoding="utf-8" (the Windows
     locale codec raises "'charmap' codec can't decode byte 0x9d" otherwise),
     and python-dotenv does read a UTF-8 .env through that argument.
  7. run_qwantonomous.bat sets PYTHONUTF8=1 before it invokes python.
"""

import os
import socket
import subprocess
import sys
import time

import pytest

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

from utils import port_reclaim
from utils.port_reclaim import (
    PortOwner,
    can_bind,
    is_our_app_process,
    listening_pids,
    probe_host,
    reclaim_port,
)


def _free_port() -> int:
    """A port that is bindable right now (grabbed then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ancestor_report(pid: int, depth: int = 6) -> str:
    """Readable ancestor chain for a pid, used in failure messages."""
    import psutil

    lines = []
    try:
        proc = psutil.Process(pid)
    except Exception as exc:  # pragma: no cover - diagnostics only
        return f"<no such process {pid}: {exc}>"

    for step in range(depth):
        proc = proc.parent() if proc is not None else None
        if proc is None:
            lines.append(f"  {step}: <no more parents>")
            break
        try:
            full = " ".join(proc.cmdline())
        except Exception:
            full = "<denied>"
        owner = port_reclaim._describe(proc.pid)
        lines.append(
            f"  {step}: pid={proc.pid} name={proc.name()} "
            f"is_ours={getattr(owner, 'is_ours', None)} "
            f"marker={port_reclaim._has_app_marker(full)} cmd={full[:120]}"
        )
    return "ancestors:\n" + "\n".join(lines)


def _wait_for_listen(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listening_pids("127.0.0.1", port):
            return True
        time.sleep(0.05)
    return False


# ── 1. address handling ───────────────────────────────────────────────────

def test_probe_host_maps_bind_only_addresses_to_loopback():
    assert probe_host("0.0.0.0") == "127.0.0.1"
    assert probe_host("::") == "127.0.0.1"
    assert probe_host("") == "127.0.0.1"
    assert probe_host(None) == "127.0.0.1"


def test_probe_host_keeps_a_real_address():
    assert probe_host("192.168.1.10") == "192.168.1.10"
    assert probe_host("127.0.0.1") == "127.0.0.1"


# ── 2. owner discovery ────────────────────────────────────────────────────

def test_free_port_is_reported_free_and_nothing_is_killed():
    port = _free_port()
    result = reclaim_port("127.0.0.1", port, purpose="http")

    assert result.reclaimed is True
    assert result.killed == ()
    assert "free" in result.reason


def test_listening_pid_is_found_and_reported_for_a_free_port():
    port = _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        pids = listening_pids("127.0.0.1", port)
        assert os.getpid() in pids, f"own listener not found in {pids}"
        assert can_bind("127.0.0.1", port) is False

    assert listening_pids("127.0.0.1", port) == ()
    assert can_bind("127.0.0.1", port) is True


# ── 3. is it OUR app? ─────────────────────────────────────────────────────

def _owner(name: str, cmdline: str, cwd: str = SERVER_ROOT, pid: int = 4242) -> PortOwner:
    return PortOwner(pid=pid, name=name, cmdline=cmdline, cwd=cwd, is_ours=False)


def test_our_own_app_process_is_recognised():
    owner = _owner("python.exe", f'python.exe "{os.path.join(SERVER_ROOT, "app.py")}"')
    assert is_our_app_process(owner) is True


def test_the_websocket_service_entry_point_is_recognised():
    owner = _owner("python.exe", "python.exe -u -m websocket_proxy.server")
    assert is_our_app_process(owner) is True


def test_a_browser_is_never_treated_as_ours():
    owner = _owner("chrome.exe", r"C:\Program Files\Google\Chrome\chrome.exe app.py")
    assert is_our_app_process(owner) is False


def test_python_running_another_projects_app_py_is_not_ours():
    owner = _owner(
        "python.exe",
        r'python.exe "C:\dev\some-other-project\app.py"',
        cwd=r"C:\dev\some-other-project",
    )
    assert is_our_app_process(owner) is False


def test_python_with_no_app_marker_is_not_ours():
    owner = _owner("python.exe", "python.exe -m pytest")
    assert is_our_app_process(owner) is False


# ── 4. never kill a live holder ───────────────────────────────────────────

def test_own_process_holding_the_port_is_never_killed():
    port = _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        result = reclaim_port("127.0.0.1", port, purpose="http")

        assert result.reclaimed is False
        assert result.killed == ()
        assert "this process" in result.reason
        # The port must still be ours and the socket untouched.
        assert listener.fileno() != -1
        assert can_bind("127.0.0.1", port) is False


def test_port_held_by_another_program_is_reported_not_killed(monkeypatch):
    """A foreign listener must produce a refusal, never a taskkill."""

    def fake_listening_pids(host, port):
        return (999_001,)

    monkeypatch.setattr(port_reclaim, "listening_pids", fake_listening_pids)
    monkeypatch.setattr(
        port_reclaim,
        "_describe",
        lambda pid, root=None: PortOwner(
            pid=pid, name="nginx.exe", cmdline="nginx.exe", cwd="C:\\nginx", is_ours=False
        ),
    )
    killed = []
    monkeypatch.setattr(port_reclaim, "_terminate", lambda pid, timeout: killed.append(pid) or True)

    result = reclaim_port("127.0.0.1", 9_999, purpose="websocket")

    assert result.reclaimed is False
    assert killed == []
    assert "another program" in result.reason
    assert "999001" in result.reason


# ── 5. a REAL stale listener is reclaimed ─────────────────────────────────

def test_a_real_stale_http_listener_is_stopped_and_the_port_freed():
    """The Flask-port case: something holds 5001 but answers nothing."""
    port = _free_port()

    # A real child process that binds the port and never serves anything. Its
    # cwd is this checkout and it carries the ``app.py`` marker on its command
    # line, which is exactly the shape of a listener left behind by a run that
    # was killed hard: ours, still bound, answering nothing.
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,time\n"
                f"s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                f"s.bind(('127.0.0.1', {port}))\n"
                "s.listen(1)\n"
                "time.sleep(120)\n"
            ),
            "app.py",
        ],
        cwd=SERVER_ROOT,
    )
    try:
        assert _wait_for_listen(port), f"child never bound port {port}"
        assert can_bind("127.0.0.1", port) is False
        assert port_reclaim.answers_request("127.0.0.1", port, timeout=1.0) is False

        result = reclaim_port("127.0.0.1", port, purpose="http", kill_timeout=16.0)

        assert result.reclaimed is True, f"{result.reason}\n{_ancestor_report(child.pid)}"
        assert result.killed, result.reason
        assert can_bind("127.0.0.1", port) is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_a_real_orphaned_websocket_listener_is_stopped():
    """The 9999 case: a WebSocket child whose app.py parent is dead.

    A "midwife" process binds the port and then EXITS, leaving the listener
    with no parent - which is exactly what an orphaned
    ``python -m websocket_proxy.server`` looks like after app.py is killed.

    The base interpreter (``sys._base_executable``) is used on purpose: the
    venv's ``python.exe`` is a redirector that stays alive as the child's
    parent, which would leave a live parent and make this test dishonest.
    """
    port = _free_port()
    base_python = getattr(sys, "_base_executable", None) or sys.executable

    listener_code = (
        "import socket,time\n"
        "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen(1)\n"
        "s.settimeout(0.2)\n"
        # Exit when nobody connects for a while, so a failed test leaks nothing.
        "deadline=time.time()+90\n"
        "while time.time()<deadline: time.sleep(0.2)\n"
    )
    midwife_code = (
        "import subprocess,sys\n"
        f"subprocess.Popen([sys.executable,'-c',{listener_code!r},'app.py'],"
        f"cwd={SERVER_ROOT!r},"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)\n"
    )
    # No pipes for the midwife either: an inherited stdout pipe would be held
    # open by the orphan and make this call wait for EOF on Windows.
    child = subprocess.run(
        [base_python, "-c", midwife_code],
        cwd=SERVER_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    assert child.returncode == 0, (child.stderr or b"").decode("utf-8", "replace")

    try:
        assert _wait_for_listen(port), f"orphan never bound port {port}"

        # Wait for the midwife to be reaped so the listener really has no parent.
        deadline = time.time() + 15
        owners = ()
        while time.time() < deadline:
            owners = tuple(
                o for o in (port_reclaim._describe(p) for p in listening_pids("127.0.0.1", port)) if o
            )
            if owners and not port_reclaim.has_live_ancestor_app(owners[0].pid, SERVER_ROOT):
                break
            time.sleep(0.2)

        assert owners, "no owner found for the orphaned listener"
        assert owners[0].is_ours is True, owners
        assert port_reclaim.has_live_ancestor_app(owners[0].pid, SERVER_ROOT) is False, (
            f"the listener still has a live parent\n{_ancestor_report(owners[0].pid)}"
        )
        assert port_reclaim.answers_request("127.0.0.1", port, timeout=1.0) is False

        result = reclaim_port("127.0.0.1", port, purpose="websocket", kill_timeout=16.0)

        assert result.reclaimed is True, f"{result.reason}\n{_ancestor_report(owners[0].pid)}"
        assert owners[0].pid in result.killed, result.reason
        assert can_bind("127.0.0.1", port) is True
    finally:
        for pid in listening_pids("127.0.0.1", port):
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass


def test_a_live_parent_app_protects_its_websocket_child(monkeypatch):
    """A WS child with a running app.py parent is serving, so it is spared."""

    def fake_listening_pids(host, port):
        return (999_002,)

    monkeypatch.setattr(port_reclaim, "listening_pids", fake_listening_pids)
    monkeypatch.setattr(
        port_reclaim,
        "_describe",
        lambda pid, root=None: PortOwner(
            pid=pid,
            name="python.exe",
            cmdline="python.exe -u -m websocket_proxy.server",
            cwd=SERVER_ROOT,
            is_ours=True,
        ),
    )
    monkeypatch.setattr(port_reclaim, "has_live_ancestor_app", lambda pid, root=None, max_depth=6: True)
    killed = []
    monkeypatch.setattr(port_reclaim, "_terminate", lambda pid, timeout: killed.append(pid) or True)

    result = reclaim_port("127.0.0.1", 9_998, purpose="websocket")

    assert result.reclaimed is False
    assert killed == []
    assert "parent is still running" in result.reason


# ── 6. .env is always read as UTF-8 ───────────────────────────────────────

_ENV_READERS = [
    "app.py",
    os.path.join("utils", "config.py"),
    os.path.join("utils", "logging.py"),
    os.path.join("utils", "env_check.py"),
    os.path.join("websocket_proxy", "app_integration.py"),
    os.path.join("websocket_proxy", "server.py"),
]


@pytest.mark.parametrize("relative", _ENV_READERS)
def test_every_dotenv_load_and_env_open_declares_utf8(relative):
    """No .env read may fall back to the locale codec (cp1252 on Windows)."""
    path = os.path.join(SERVER_ROOT, relative)
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()

    problems = []
    for number, line in enumerate(lines, start=1):
        stripped = line.split("#", 1)[0]
        if "load_dotenv(" in stripped and "encoding=" not in stripped:
            problems.append((number, "load_dotenv", line.strip()))
        if "open(" in stripped and "encoding=" not in stripped:
            # Only .env-ish opens matter here; other files are out of scope.
            if any(token in stripped for token in ("env_path", "ENV_PATH", "sample_env_path", ".env")):
                problems.append((number, "open", line.strip()))

    assert not problems, f"{relative} reads .env without encoding='utf-8': {problems}"


def test_utf8_env_with_punctuation_loads_through_dotenv(tmp_path):
    """A .env carrying a smart quote loads instead of raising a charmap error."""
    from dotenv import dotenv_values

    env_file = tmp_path / ".env"
    env_file.write_text(
        "ENV_CONFIG_VERSION='2.0.2.1'\n"
        "# a comment with \u2019 and \u2014 and \u201cquotes\u201d\n"
        "FLASK_PORT='5001'\n",
        encoding="utf-8",
    )
    # 0x9d is the byte the Windows locale codec choked on; make sure it is
    # really there so this test cannot pass on a plain-ASCII file. The right
    # double quote (U+201D) is e2 80 9d in UTF-8.
    assert b"\x9d" in env_file.read_bytes(), "test fixture lost the 0x9d byte"

    values = dotenv_values(env_file, encoding="utf-8")

    assert values["FLASK_PORT"] == "5001"
    assert values["ENV_CONFIG_VERSION"] == "2.0.2.1"


def test_version_check_function_opens_dotenv_as_utf8():
    """The charmap warning came from check_env_version_compatibility's opens."""
    from utils import env_check

    with open(env_check.__file__, encoding="utf-8") as handle:
        source = handle.read()

    start = source.index("def check_env_version_compatibility")
    end = source.index("def ", source.index("return True", start))
    body = source[start:end]

    assert 'open(env_path, encoding="utf-8")' in body
    assert 'open(sample_env_path, encoding="utf-8")' in body


# ── 7. the launcher forces UTF-8 mode ─────────────────────────────────────

def test_launcher_sets_pythonutf8_before_python_runs():
    bat = os.path.join(SERVER_ROOT, "run_qwantonomous.bat")
    with open(bat, encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()

    joined = "".join(lines).lower()
    assert 'set "pythonutf8=1"' in joined or "set pythonutf8=1" in joined

    set_index = next(
        index for index, line in enumerate(lines) if "pythonutf8=1" in line.lower()
    )
    python_run = next(
        index
        for index, line in enumerate(lines)
        if "%python%" in line.lower() and "app.py" in line.lower()
    )
    assert set_index < python_run, "PYTHONUTF8 must be set before python runs app.py"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
