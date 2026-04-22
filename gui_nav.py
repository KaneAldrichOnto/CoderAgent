#!/usr/bin/env python3
"""
gui_nav.py - Generic Windows UIA automation server (app-agnostic)
=================================================================

A persistent TCP server that holds a pywinauto UIA connection and a
control cache for a single target Windows process, so each CLI
invocation gets sub-second responses. Every subcommand is a thin
client that talks to the server over a local TCP socket; the server
auto-starts on the first call.

Target process is REQUIRED. Specify it in one of two ways:

  1. CLI flag (highest precedence):  --process MyApp.exe
  2. CoderAgentConfig.yaml in cwd:   target_process: MyApp.exe

If neither is set, the server refuses to start.

Optional dependencies (only needed for real subcommands; --help works
without them):

    pip install pywinauto pillow pywin32

Example:

    python gui_nav.py serve --process Notepad.exe
    python gui_nav.py click "File"
    python gui_nav.py screenshot --path out.png

Subcommands:

    serve, tree, click, click-xy, right-click, inspect, find, text,
    set-text, send-keys, scroll, wait-for, navigate, screenshot,
    refresh, status, stop, alias, prepare-rdp

The server keeps a multi-window cache (main window + visible dialogs),
performs a 4-pass control lookup (exact UIA name, substring UIA name,
exact alias, substring alias), and supports four click strategies
(auto/sendinput/postmessage/uia). It captures screenshots via the
PrintWindow API so it works over RDP and locked sessions; an optional
prepare-rdp subcommand installs a Windows scheduled task that
reconnects the session to the physical console after RDP disconnect.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional dependencies. Wrapped so --help works on machines without them
# (and on non-Windows hosts). Real subcommands check these flags and bail
# out with a clear message before doing anything that needs the import.
# ---------------------------------------------------------------------------
try:
    import ctypes
    import ctypes.wintypes  # noqa: F401  (used inside class methods)
    _HAS_CTYPES = True
except Exception:
    ctypes = None  # type: ignore
    _HAS_CTYPES = False

try:
    import pywinauto  # noqa: F401
    _HAS_PYWINAUTO = True
except Exception:
    _HAS_PYWINAUTO = False

try:
    from PIL import Image as PILImage  # noqa: F401
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

try:
    import win32gui  # noqa: F401
    import win32con  # noqa: F401
    import win32api  # noqa: F401
    import win32process  # noqa: F401
    import win32ui  # noqa: F401
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False


# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------
DEFAULT_PORT = 8765
HOST = "127.0.0.1"
IDLE_TIMEOUT = 1800  # server auto-exits after 30 min idle
DEFAULT_LOG_FILE = "gui_nav_server.log"
CONFIG_FILE_NAME = "CoderAgentConfig.yaml"

# Control types worth showing by default in `tree` output.
_CONTROL_TYPES = frozenset([
    "TabItem", "Tab", "Button", "CheckBox", "RadioButton",
    "ComboBox", "Edit", "Text", "TreeItem", "ListItem",
    "MenuItem", "Menu", "Hyperlink", "Group", "Pane",
])


# ---------------------------------------------------------------------------
# Config loading (dependency-free YAML subset; mirrors setup.py)
# ---------------------------------------------------------------------------

def _parse_simple_yaml(text):
    """Parse a flat ``key: value`` YAML file (no nested structures)."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)", line)
        if m:
            result[m.group(1)] = m.group(2).strip().strip("'\"")
    return result


def _load_config():
    """Return parsed config from ``./CoderAgentConfig.yaml`` (cwd), or {}."""
    cfg_path = Path.cwd() / CONFIG_FILE_NAME
    if not cfg_path.is_file():
        return {}
    try:
        return _parse_simple_yaml(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_target_process(cli_value):
    """Pick the target process name. CLI > config. Returns None if unset."""
    if cli_value:
        return cli_value
    cfg = _load_config()
    val = cfg.get("target_process", "").strip()
    return val or None


def _resolve_port(cli_value):
    """Pick the TCP port. CLI > config > DEFAULT_PORT."""
    if cli_value:
        return int(cli_value)
    cfg = _load_config()
    val = cfg.get("gui_nav_port", "").strip()
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return DEFAULT_PORT


# ---------------------------------------------------------------------------
# Dependency-guard helpers
# ---------------------------------------------------------------------------

def _require_windows_runtime():
    """Verify we're on Windows with all GUI deps available; print + exit if not."""
    if sys.platform != "win32":
        print("ERROR: gui_nav.py GUI automation is Windows-only.", file=sys.stderr)
        sys.exit(2)
    missing = []
    if not _HAS_PYWINAUTO:
        missing.append("pywinauto")
    if not _HAS_PIL:
        missing.append("pillow")
    if not _HAS_WIN32:
        missing.append("pywin32")
    if missing:
        print("ERROR: Required packages are not installed: "
              + ", ".join(missing), file=sys.stderr)
        print("Install with: pip install " + " ".join(missing), file=sys.stderr)
        sys.exit(2)


# ===========================================================================
#  Server
# ===========================================================================

class GUINavServer:
    """Persistent server holding a pywinauto UIA connection + control cache
    for a single target process."""

    def __init__(self, target_process, port=DEFAULT_PORT, log_file=None):
        if not target_process:
            raise ValueError("target_process is required")
        self.target_process = target_process
        self.port = int(port)
        self.log_file = Path(log_file or DEFAULT_LOG_FILE).resolve()

        self.app = None
        self.win = None
        self._pid = None
        self._cache = None
        self._cache_time = 0
        self._lock = threading.Lock()
        self._last_activity = time.time()
        self._start_time = time.time()
        self._manual_aliases = {}  # alias_lower -> uia_name

        # Session monitor (RDP disconnect tracking)
        self._session_active = threading.Event()
        self._session_active.set()
        self._session_state = "active"
        self._session_id = 0

    # ---- Logging --------------------------------------------------------

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ---- Session monitoring (RDP) --------------------------------------

    @staticmethod
    def _get_session_id():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        sid = ctypes.c_ulong()
        kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        return sid.value

    @staticmethod
    def _query_session_state(sid):
        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        WTSConnectState = 8
        buf = ctypes.c_void_p()
        n = ctypes.c_ulong()
        ok = wtsapi32.WTSQuerySessionInformationW(
            0, sid, WTSConnectState,
            ctypes.byref(buf), ctypes.byref(n),
        )
        if not ok:
            return None
        state = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
        wtsapi32.WTSFreeMemory(buf)
        return state

    def _invalidate_connection(self):
        self.win = None
        self.app = None
        self._cache = None

    def _start_session_monitor(self):
        if sys.platform != "win32":
            return
        self._session_id = self._get_session_id()

        def _monitor():
            last_state = 0  # Active
            while True:
                time.sleep(3)
                try:
                    sid = self._get_session_id()
                    if sid != self._session_id:
                        self._session_id = sid
                        self._log(f"Session ID changed to {sid}")
                    state = self._query_session_state(sid)
                    if state is None:
                        continue
                    was_active = last_state in (0, 1)
                    is_active = state in (0, 1)
                    if was_active and not is_active:
                        self._session_active.clear()
                        self._session_state = "disconnected"
                        self._log(f"Session {sid} DISCONNECTED (state={state})")
                        with self._lock:
                            self._invalidate_connection()
                    elif not was_active and is_active:
                        self._log("Session RECONNECTED, waiting 1.5s for DWM...")
                        time.sleep(1.5)
                        with self._lock:
                            self._invalidate_connection()
                        self._session_state = "active"
                        self._session_active.set()
                        self._log("Session active, cache invalidated")
                    last_state = state
                except Exception as e:
                    self._log(f"Session monitor error: {e}")

        th = threading.Thread(target=_monitor, daemon=True,
                              name="session-monitor")
        th.start()
        self._log(f"Session monitor started (session {self._session_id})")

    def _ensure_session_active(self):
        if sys.platform != "win32":
            return True
        if self._session_active.is_set():
            return True
        self._log("Session disconnected, waiting up to 30s for recovery...")
        if self._session_active.wait(timeout=30):
            self._log("Session recovered")
            return True
        self._log("Session still disconnected after 30s.")
        return False

    # ---- Connection ----------------------------------------------------

    def _find_pid(self):
        """Return the PID of a running process matching ``self.target_process``.

        Match is case-insensitive against the executable basename; ``.exe``
        suffix is optional. Raises RuntimeError if no match."""
        wanted = self.target_process.lower()
        if not wanted.endswith(".exe"):
            wanted_alt = wanted + ".exe"
        else:
            wanted_alt = wanted[:-4]

        # Use PowerShell Get-Process for reliability across Windows versions.
        # Strip extension because Get-Process expects the basename without .exe.
        bare = wanted[:-4] if wanted.endswith(".exe") else wanted
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Process -Name '{bare}' -ErrorAction Stop | "
                 "Select-Object -First 1).Id"],
                text=True, timeout=10,
            ).strip()
            if out:
                return int(out.splitlines()[0])
        except subprocess.CalledProcessError:
            pass
        except Exception:
            pass
        raise RuntimeError(
            f"Process not found: {self.target_process!r} (also tried "
            f"{wanted_alt!r}). Is it running?"
        )

    def _connect(self):
        from pywinauto import Application
        self._pid = self._find_pid()
        self.app = Application(backend="uia").connect(process=self._pid)
        for w in self.app.windows():
            if w.element_info.control_type == "Window":
                self.win = w
                return
        raise RuntimeError(
            f"No top-level Window found for process {self.target_process!r}"
        )

    def ensure_connected(self):
        if not self._ensure_session_active():
            raise RuntimeError(
                "RDP session is disconnected and could not reconnect. "
                "Reconnect via RDP or run 'prepare-rdp'."
            )
        if self.win is not None:
            try:
                self.win.element_info.name
                self._ensure_restored()
                return
            except Exception:
                self._invalidate_connection()
        last = None
        for attempt in range(3):
            try:
                self._connect()
                self._ensure_restored()
                return
            except Exception as e:
                last = e
                self._log(f"Connection attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2)
        raise RuntimeError(f"Failed to connect after 3 attempts: {last}")

    def _ensure_restored(self):
        """Restore the window if minimized (SW_SHOWNOACTIVATE, no focus steal)."""
        if self.win is None or not _HAS_WIN32:
            return
        try:
            hwnd = self.win.handle
            if hwnd and win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
                time.sleep(0.3)
                self._cache = None
                self._log("Restored minimized window")
        except Exception as e:
            self._log(f"Warning: could not restore window: {e}")

    # ---- Cache ---------------------------------------------------------

    @staticmethod
    def _get_zorder(hwnd):
        if sys.platform != "win32":
            return 0
        try:
            user32 = ctypes.windll.user32
            GW_HWNDPREV = 3
            rank = 0
            h = hwnd
            while True:
                h = user32.GetWindow(h, GW_HWNDPREV)
                if not h:
                    break
                rank += 1
            return rank
        except Exception:
            return 0

    def _get_all_windows(self):
        """All visible top-level windows of the target process. Dialogs first
        (sorted by z-order, topmost first), then the main window."""
        wins = []
        try:
            for w in self.app.windows():
                try:
                    r = w.element_info.rectangle
                    if (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
                        wins.append(w)
                except Exception:
                    pass
        except Exception:
            pass
        main_h = self.win.handle if self.win else None
        dialogs = [w for w in wins if w.handle != main_h]
        mains = [w for w in wins if w.handle == main_h]
        dialogs.sort(key=lambda w: self._get_zorder(w.handle))
        return dialogs + mains

    def get_cache(self, refresh=False):
        if self._cache is not None and not refresh:
            return self._cache
        self.ensure_connected()
        t = time.time()

        windows = self._get_all_windows() or [self.win]
        cache = []
        for win in windows:
            wh = win.handle
            try:
                wt = win.element_info.name or ""
            except Exception:
                wt = ""
            wz = self._get_zorder(wh)
            result = [None]

            def _enum(w=win):
                try:
                    result[0] = list(w.descendants())
                except Exception as e:
                    self._log(f"Warning: descendants failed: {e}")

            th = threading.Thread(target=_enum, daemon=True)
            th.start()
            th.join(timeout=15.0)
            if th.is_alive():
                self._log(f"Warning: descendants() timed out for {wt!r}")
                continue
            for ctrl in (result[0] or []):
                try:
                    ei = ctrl.element_info
                    r = ei.rectangle
                    cache.append((ctrl, ei.name or "", ei.control_type,
                                  (r.left, r.top, r.right, r.bottom),
                                  wh, wt, wz))
                except Exception:
                    pass

        self._cache = cache
        self._cache_time = time.time()
        self._log(f"Cache built: {len(cache)} controls from "
                  f"{len(windows)} window(s) in {self._cache_time - t:.1f}s")
        return self._cache

    def _get_control_path(self, ctrl):
        parts = []
        cur = ctrl
        try:
            while cur is not None:
                ei = cur.element_info
                ctype = ei.control_type or "?"
                cname = ei.name or ""
                parts.append(f"{ctype}:{cname}" if cname else ctype)
                cur = cur.parent()
        except Exception:
            pass
        parts.reverse()
        return " > ".join(parts)

    def find_control(self, name, control_type="", index=0):
        """4-pass lookup. Returns (chosen_ctrl, all_matches_in_winning_pass).
        Passes:
          1. Exact UIA name
          2. Substring UIA name
          3. Exact alias match -> resolved UIA name
          4. Substring alias match -> resolved UIA name(s)
        """
        cache = self.get_cache()
        name_lower = name.lower()

        def _vis(r):
            return r[2] > r[0] and r[3] > r[1]

        def _ct(t):
            return not control_type or t == control_type

        def _pick(matches):
            if not matches:
                return None, []
            info = []
            for c in matches:
                path = self._get_control_path(c)
                wt, wz = "", 0
                for cc, _cn, _ct2, _r, _wh, wt2, wz2 in cache:
                    if cc is c:
                        wt, wz = wt2, wz2
                        break
                info.append((c, path, wt, wz))
            chosen = matches[index] if index < len(matches) else matches[0]
            return chosen, info

        # 1. Exact UIA name
        m = [c for c, cn, ct, r, _wh, _wt, _wz in cache
             if cn == name and _ct(ct) and _vis(r)]
        if m:
            return _pick(m)

        # 2. Substring UIA name
        m = [c for c, cn, ct, r, _wh, _wt, _wz in cache
             if name_lower in cn.lower() and _ct(ct) and _vis(r)]
        if m:
            return _pick(m)

        # 3. Exact alias match
        if name_lower in self._manual_aliases:
            uia = self._manual_aliases[name_lower]
            m = [c for c, cn, ct, r, _wh, _wt, _wz in cache
                 if cn == uia and _ct(ct) and _vis(r)]
            if m:
                return _pick(m)

        # 4. Substring alias match
        sub = {self._manual_aliases[k] for k in self._manual_aliases
               if name_lower in k}
        if sub:
            m = [c for c, cn, ct, r, _wh, _wt, _wz in cache
                 if cn in sub and _ct(ct) and _vis(r)]
            if m:
                return _pick(m)

        return None, []

    # ---- Command dispatch ----------------------------------------------

    def handle(self, req):
        self._last_activity = time.time()
        cmd = req.get("cmd", "")
        handler = {
            "ping": self._cmd_ping,
            "tree": self._cmd_tree,
            "click": self._cmd_click,
            "click_xy": self._cmd_click_xy,
            "inspect": self._cmd_inspect,
            "text": self._cmd_text,
            "refresh": self._cmd_refresh,
            "status": self._cmd_status,
            "screenshot": self._cmd_screenshot,
            "set_text": self._cmd_set_text,
            "right_click": self._cmd_right_click,
            "wait_for": self._cmd_wait_for,
            "send_keys": self._cmd_send_keys,
            "scroll": self._cmd_scroll,
            "navigate": self._cmd_navigate,
            "batch": self._cmd_batch,
            "alias": self._cmd_alias,
            "find": self._cmd_find,
            "stop": self._cmd_stop,
        }.get(cmd)
        if handler is None:
            return {"ok": False, "result": f"Unknown command: {cmd}"}
        try:
            return handler(req)
        except Exception as e:
            if not self._session_active.is_set():
                self._log(f"Command '{cmd}' failed during disconnect: {e}. "
                          "Waiting for recovery...")
                if self._session_active.wait(timeout=30):
                    try:
                        return handler(req)
                    except Exception as e2:
                        return {"ok": False, "result": str(e2)}
            self._log(f"Error handling {cmd}: {e}")
            return {"ok": False, "result": str(e)}

    # ---- Click strategies ----------------------------------------------

    def _uia_click(self, ctrl):
        """Try UIA patterns. Returns True if a pattern worked."""
        ei = ctrl.element_info
        ctype = ei.control_type
        try:
            iface = ctrl.iface_expand_collapse
            from pywinauto.uia_defines import expand_collapse_state_collapsed
            if iface.CurrentExpandCollapseState == expand_collapse_state_collapsed:
                iface.Expand()
            else:
                iface.Collapse()
            return True
        except Exception:
            pass
        if ctype in ("CheckBox", "RadioButton"):
            try:
                ctrl.toggle()
                return True
            except Exception:
                pass
        if ctype in ("TabItem", "ListItem"):
            try:
                ctrl.select()
                if hasattr(ctrl, "is_selected") and not ctrl.is_selected():
                    pass
                else:
                    return True
            except Exception:
                pass
        # Skip Invoke for Button / MenuItem -- it deadlocks on modal dialogs.
        if ctype not in ("MenuItem", "Button"):
            try:
                ctrl.invoke()
                return True
            except Exception:
                pass
        return False

    def _post_click(self, ctrl):
        """Click via PostMessage. Works in RDP / disconnected sessions."""
        ei = ctrl.element_info
        hwnd = ctrl.handle
        if hwnd:
            BM_CLICK = 0x00F5
            win32gui.PostMessage(hwnd, BM_CLICK, 0, 0)
            return
        r = ei.rectangle
        mid_x = (r.left + r.right) // 2
        mid_y = (r.top + r.bottom) // 2
        parent = ctrl.parent()
        hwnd = (parent.handle if parent else None) or self.win.handle
        client_x, client_y = win32gui.ScreenToClient(hwnd, (mid_x, mid_y))
        lParam = (client_y << 16) | (client_x & 0xFFFF)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN,
                             win32con.MK_LBUTTON, lParam)
        time.sleep(0.05)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)

    def _sendinput_at(self, x, y, right=False, bring_to_front=True):
        """Real Win32 SendInput click at absolute screen coords. Required for
        popup menus and SplitButton chevrons that ignore PostMessage."""
        user32 = ctypes.windll.user32
        main_hwnd = self.win.handle
        target_pid = win32process.GetWindowThreadProcessId(main_hwnd)[1]
        target_hwnd = main_hwnd
        try:
            child = win32gui.WindowFromPoint((int(x), int(y)))
            if child:
                top = child
                while True:
                    parent = win32gui.GetAncestor(top, 2)  # GA_ROOT
                    if not parent or parent == top:
                        break
                    top = parent
                if top:
                    top_pid = win32process.GetWindowThreadProcessId(top)[1]
                    if top_pid == target_pid:
                        target_hwnd = top
        except Exception:
            pass
        target_tid, _ = win32process.GetWindowThreadProcessId(target_hwnd)
        my_tid = win32api.GetCurrentThreadId()
        fg_hwnd = user32.GetForegroundWindow()
        fg_tid, fg_pid = (0, 0)
        if fg_hwnd and fg_hwnd != target_hwnd:
            fg_tid, fg_pid = win32process.GetWindowThreadProcessId(fg_hwnd)
        # If foreground already belongs to the target process (e.g. a popup
        # menu), don't re-activate -- it would dismiss the menu.
        skip_activate = bring_to_front and fg_pid == target_pid
        if fg_tid and fg_tid != my_tid and fg_tid != target_tid:
            user32.AttachThreadInput(my_tid, fg_tid, True)
        user32.AttachThreadInput(my_tid, target_tid, True)
        try:
            if bring_to_front and not skip_activate:
                user32.SwitchToThisWindow(target_hwnd, True)
                user32.BringWindowToTop(target_hwnd)
                user32.SetForegroundWindow(target_hwnd)
                time.sleep(0.1)
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.05)

            INPUT_MOUSE = 0

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ('dx', ctypes.c_long), ('dy', ctypes.c_long),
                    ('mouseData', ctypes.wintypes.DWORD),
                    ('dwFlags', ctypes.wintypes.DWORD),
                    ('time', ctypes.wintypes.DWORD),
                    ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUT(ctypes.Structure):
                class _U(ctypes.Union):
                    _fields_ = [('mi', MOUSEINPUT)]
                _fields_ = [
                    ('type', ctypes.wintypes.DWORD), ('ii', _U),
                ]

            def _send(flags):
                inp = INPUT()
                inp.type = INPUT_MOUSE
                inp.ii.mi.dwFlags = flags
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

            down = 0x0008 if right else 0x0002
            up = 0x0010 if right else 0x0004
            _send(down)
            time.sleep(0.03)
            _send(up)
        finally:
            user32.AttachThreadInput(my_tid, target_tid, False)
            if fg_tid and fg_tid != my_tid and fg_tid != target_tid:
                user32.AttachThreadInput(my_tid, fg_tid, False)

    def _sendinput_click(self, ctrl):
        r = ctrl.element_info.rectangle
        self._sendinput_at((r.left + r.right) // 2,
                           (r.top + r.bottom) // 2, right=False)

    def _click_control(self, ctrl, method="auto"):
        self.ensure_connected()
        method = (method or "auto").lower()
        if method == "sendinput":
            self._sendinput_click(ctrl)
            return "SendInput"
        if method == "postmessage":
            self._post_click(ctrl)
            return "PostMessage"
        if method == "uia":
            return "UIA" if self._uia_click(ctrl) else "UIA(no-pattern)"
        ctype = ctrl.element_info.control_type
        if ctype == "ComboBox":
            self._sendinput_click(ctrl)
            return "SendInput"
        if self._uia_click(ctrl):
            return "UIA"
        self._post_click(ctrl)
        return "PostMessage"

    @staticmethod
    def _get_control_state(ctrl, ctype):
        try:
            if ctype in ("CheckBox", "RadioButton"):
                return f" [{'checked' if ctrl.get_toggle_state() else 'unchecked'}]"
            if ctype == "TabItem":
                return " [selected]"
        except Exception:
            pass
        return ""

    @staticmethod
    def _format_match_warning(all_matches, name, index):
        if len(all_matches) <= 1:
            return ""
        lines = [f"\nWARNING: {len(all_matches)} controls matched '{name}'"]
        for idx, (_m, path, wt, wz) in enumerate(all_matches):
            mark = " <-- selected" if idx == index else ""
            wlabel = f' (window="{wt}" z={wz})' if wt else ""
            lines.append(f"  [{idx}] {path}{wlabel}{mark}")
        lines.append("Use --index N to select a different match.")
        return "\n".join(lines)

    # ---- Command handlers ----------------------------------------------

    def _cmd_ping(self, req):
        return {"ok": True, "result": "pong",
                "start_time": self._start_time}

    def _cmd_tree(self, req):
        name_filter = req.get("name", "").lower()
        ct_filter = req.get("type", "")
        cache = self.get_cache(refresh=req.get("refresh", False))
        lines = []
        for ctrl, cname, ctype, rect, _wh, _wt, _wz in cache:
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            if ct_filter:
                if ctype != ct_filter:
                    continue
            elif ctype not in _CONTROL_TYPES:
                continue
            if name_filter and name_filter not in cname.lower():
                continue
            state = ""
            try:
                state = (" [checked]" if ctrl.get_toggle_state()
                         else " [unchecked]")
            except Exception:
                pass
            try:
                if ctype == "TabItem" and hasattr(ctrl, "is_selected"):
                    if ctrl.is_selected():
                        state = " [selected]"
            except Exception:
                pass
            lines.append(
                f'  {ctype}: "{cname}" '
                f'rect=({rect[0]},{rect[1]},{rect[2]},{rect[3]}){state}')
        if not lines:
            return {"ok": True,
                    "result": f"No controls matching name='{req.get('name', '')}' "
                              f"type='{ct_filter}'"}
        return {"ok": True,
                "result": f"Found {len(lines)} controls:\n" + "\n".join(lines)}

    def _cmd_click(self, req):
        name = req.get("name", "")
        ct = req.get("type", "")
        index = req.get("index", 0)
        if req.get("refresh"):
            self._cache = None
        ctrl, all_matches = self.find_control(name, ct, index=index)
        if not ctrl:
            return {"ok": False,
                    "result": f"Not found: name='{name}' type='{ct}'"}
        ei = ctrl.element_info
        ctype = ei.control_type
        cname = ei.name or ""
        r = ei.rectangle
        cm = self._click_control(ctrl, method=req.get("method", "auto"))
        time.sleep(0.3)
        state = self._get_control_state(ctrl, ctype)
        result = (f'Clicked {ctype}: "{cname}" '
                  f'at ({r.left},{r.top},{r.right},{r.bottom}){state} via {cm}')
        result += self._format_match_warning(all_matches, name, index)
        return {"ok": True, "result": result}

    def _cmd_click_xy(self, req):
        self.ensure_connected()
        try:
            x = int(req.get("x"))
            y = int(req.get("y"))
        except (TypeError, ValueError):
            return {"ok": False, "result": "click_xy requires integer x and y"}
        right = bool(req.get("right", False))
        self._sendinput_at(x, y, right=right)
        time.sleep(0.3)
        return {"ok": True,
                "result": f"SendInput {'right' if right else 'left'}-click "
                          f"at ({x},{y})"}

    def _cmd_inspect(self, req):
        ctrl, _ = self.find_control(req.get("name", ""), req.get("type", ""))
        if not ctrl:
            return {"ok": False,
                    "result": f"Not found: name='{req.get('name', '')}' "
                              f"type='{req.get('type', '')}'"}
        ei = ctrl.element_info
        r = ei.rectangle
        lines = [
            f'Control: {ei.control_type}: "{ei.name}"',
            f'Rect: ({r.left},{r.top},{r.right},{r.bottom})',
            f'Enabled: {ei.enabled}, Visible: {ei.visible}',
        ]
        try:
            lines.append(f"Toggle: {ctrl.get_toggle_state()}")
        except Exception:
            pass
        try:
            if hasattr(ctrl, "is_selected"):
                lines.append(f"Selected: {ctrl.is_selected()}")
        except Exception:
            pass
        try:
            txt = ctrl.window_text()
            if txt:
                lines.append(f"Text: {txt}")
        except Exception:
            pass
        children = list(ctrl.children())
        if children:
            lines.append(f"Children ({len(children)}):")
            for ch in children[:30]:
                ce = ch.element_info
                cr = ce.rectangle
                lines.append(
                    f'  {ce.control_type}: "{ce.name or "(unnamed)"}" '
                    f'rect=({cr.left},{cr.top},{cr.right},{cr.bottom})')
        return {"ok": True, "result": "\n".join(lines)}

    def _cmd_text(self, req):
        name = req.get("name", "")
        ct = req.get("type", "")
        max_items = req.get("max", 200)
        if name:
            ctrl, _ = self.find_control(name, ct)
            if not ctrl:
                return {"ok": False,
                        "result": f"Not found: name='{name}' type='{ct}'"}
            root = ctrl
            use_cache = False
        else:
            self.ensure_connected()
            root = self.win
            use_cache = True

        rei = root.element_info
        rr = rei.rectangle
        lines = [
            f'Root: {rei.control_type}: "{rei.name or "(unnamed)"}" '
            f'rect=({rr.left},{rr.top},{rr.right},{rr.bottom})',
            "",
        ]
        count = 0
        interactive_set = ("Button", "CheckBox", "RadioButton", "TabItem",
                           "ComboBox", "Edit", "MenuItem", "Hyperlink")

        if use_cache and self._cache is not None:
            for ctrl_w, cname, ctype, rect, _wh, _wt, _wz in self._cache:
                if count >= max_items:
                    lines.append(f"  ... truncated at {max_items}")
                    break
                if rect[2] <= rect[0] or rect[3] <= rect[1]:
                    continue
                if not cname and ctype not in interactive_set:
                    continue
                state = ""
                try:
                    if ctype in ("CheckBox", "RadioButton"):
                        state = (" [checked]" if ctrl_w.get_toggle_state()
                                 else " [unchecked]")
                    elif ctype == "TabItem" and hasattr(ctrl_w, "is_selected"):
                        state = " [selected]" if ctrl_w.is_selected() else ""
                except Exception:
                    pass
                text = cname or "(no text)"
                lines.append(
                    f'  {ctype}: "{text}" '
                    f'rect=({rect[0]},{rect[1]},{rect[2]},{rect[3]}){state}')
                count += 1
        else:
            for ctrl_d in root.descendants():
                if count >= max_items:
                    lines.append(f"  ... truncated at {max_items}")
                    break
                ei = ctrl_d.element_info
                ctype = ei.control_type
                cname = ei.name or ""
                r = ei.rectangle
                if r.right <= r.left or r.bottom <= r.top:
                    continue
                parts = []
                if cname:
                    parts.append(cname)
                try:
                    wt = ctrl_d.window_text()
                    if wt and wt != cname:
                        parts.append(wt)
                except Exception:
                    pass
                if not parts and ctype not in interactive_set:
                    continue
                state = ""
                try:
                    if ctype in ("CheckBox", "RadioButton"):
                        state = (" [checked]" if ctrl_d.get_toggle_state()
                                 else " [unchecked]")
                    elif ctype == "TabItem" and hasattr(ctrl_d, "is_selected"):
                        state = " [selected]" if ctrl_d.is_selected() else ""
                except Exception:
                    pass
                text = " | ".join(parts) if parts else "(no text)"
                lines.append(
                    f'  {ctype}: "{text}" '
                    f'rect=({r.left},{r.top},{r.right},{r.bottom}){state}')
                count += 1
        lines[1] = f"Visible controls: {count}"
        return {"ok": True, "result": "\n".join(lines)}

    def _cmd_refresh(self, req):
        cache = self.get_cache(refresh=True)
        return {"ok": True, "result": f"Refreshed: {len(cache)} controls"}

    def _cmd_status(self, req):
        uptime = time.time() - self._start_time
        cache_size = len(self._cache) if self._cache else 0
        cache_age = time.time() - self._cache_time if self._cache_time else -1
        lines = [
            f"Target process: {self.target_process}",
            f"Server uptime: {uptime:.0f}s",
            f"Connected: {self.win is not None}",
            f"Session: {self._session_state} (id={self._session_id})",
            f"Cache: {cache_size} controls",
            (f"Cache age: {cache_age:.0f}s" if cache_age >= 0
             else "Cache: not built"),
            f"Aliases: {len(self._manual_aliases)}",
        ]
        return {"ok": True, "result": "\n".join(lines),
                "start_time": self._start_time}

    @staticmethod
    def _is_image_black(img):
        w, h = img.size
        if w == 0 or h == 0:
            return True
        for i in range(25):
            x = int(w * ((i * 7 + 3) % 25) / 25)
            y = int(h * ((i * 11 + 5) % 25) / 25)
            p = img.getpixel((x, y))
            if isinstance(p, tuple):
                if any(c > 10 for c in p[:3]):
                    return False
            elif p > 10:
                return False
        return True

    def _write_view_sibling(self, composite, save_path, view_max):
        """Aspect-preserving Lanczos downsample alongside the original PNG.
        Returns (view_path, (w, h), scale) or None."""
        try:
            w, h = composite.size
            long_edge = max(w, h)
            if view_max <= 0 or long_edge <= view_max:
                return None
            scale = view_max / float(long_edge)
            new_size = (max(1, int(round(w * scale))),
                        max(1, int(round(h * scale))))
            view_path = str(Path(save_path).with_suffix(".view.png"))
            sidecar_path = str(Path(save_path).with_suffix(".view.json"))
            ds = composite.resize(new_size, PILImage.LANCZOS)
            ds.save(view_path, format="PNG", optimize=True)
            sidecar = {
                "original": Path(save_path).name,
                "original_size": [w, h],
                "view_size": [new_size[0], new_size[1]],
                "scale_view_to_original": w / float(new_size[0]),
            }
            Path(sidecar_path).write_text(json.dumps(sidecar, indent=2),
                                          encoding="utf-8")
            return view_path, new_size, scale
        except Exception as e:
            self._log(f"Warning: failed to write view sibling: {e}")
            return None

    def _cmd_screenshot(self, req):
        """Capture all visible windows of the target process via PrintWindow,
        composited onto a single image."""
        self.ensure_connected()
        _, pid = win32process.GetWindowThreadProcessId(self.win.handle)
        hwnds = []

        def _enum(hwnd, _):
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid == pid and win32gui.IsWindowVisible(hwnd):
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w > 0 and h > 0:
                    hwnds.append((hwnd, rect, w, h))

        win32gui.EnumWindows(_enum, None)
        if not hwnds:
            return {"ok": False, "result": "No visible window found"}
        all_left = min(r[0] for _, r, _, _ in hwnds)
        all_top = min(r[1] for _, r, _, _ in hwnds)
        all_right = max(r[2] for _, r, _, _ in hwnds)
        all_bottom = max(r[3] for _, r, _, _ in hwnds)
        total_w = all_right - all_left
        total_h = all_bottom - all_top

        composite = PILImage.new('RGB', (total_w, total_h), (0, 0, 0))
        hwnds.sort(key=lambda x: x[2] * x[3], reverse=True)

        for hwnd, rect, w, h in hwnds:
            try:
                hwndDC = win32gui.GetWindowDC(hwnd)
                mfcDC = win32ui.CreateDCFromHandle(hwndDC)
                saveDC = mfcDC.CreateCompatibleDC()
                saveBitMap = win32ui.CreateBitmap()
                saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)
                saveDC.SelectObject(saveBitMap)
                ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)
                bmpinfo = saveBitMap.GetInfo()
                bmpstr = saveBitMap.GetBitmapBits(True)
                img = PILImage.frombuffer(
                    'RGB', (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                    bmpstr, 'raw', 'BGRX', 0, 1)
                composite.paste(img, (rect[0] - all_left, rect[1] - all_top))
                win32gui.DeleteObject(saveBitMap.GetHandle())
                saveDC.DeleteDC()
                mfcDC.DeleteDC()
                win32gui.ReleaseDC(hwnd, hwndDC)
            except Exception as e:
                self._log(f"Warning: failed to capture HWND {hwnd}: {e}")

        if self._is_image_black(composite) and not req.get("_retry"):
            self._log("Screenshot all-black, waiting 2s for DWM...")
            time.sleep(2)
            r2 = dict(req)
            r2["_retry"] = True
            return self._cmd_screenshot(r2)

        save_path = req.get("path") or str(
            Path.cwd() / "screenshots" / "_last_screenshot.png")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        composite.save(save_path, format="PNG")

        warn = ""
        if self._is_image_black(composite):
            warn = " WARNING: image appears all-black (no display?)"

        view_msg = ""
        if not req.get("no_view"):
            vmax = int(req.get("view_max", 1280))
            vi = self._write_view_sibling(composite, save_path, vmax)
            if vi:
                vp, (vw, vh), _s = vi
                view_msg = f"\n  View ({vw}x{vh}): {vp}"

        return {"ok": True,
                "result": (f"Screenshot saved ({total_w}x{total_h}, "
                           f"{len(hwnds)} windows): {save_path}{warn}{view_msg}")}

    def _cmd_set_text(self, req):
        ctrl, _ = self.find_control(req.get("name", ""), req.get("type", ""))
        if not ctrl:
            return {"ok": False,
                    "result": f"Not found: name='{req.get('name', '')}'"}
        ei = ctrl.element_info
        ctype = ei.control_type
        cname = ei.name or ""
        value = req.get("value", "")
        append = req.get("append", False)
        try:
            iface = ctrl.iface_value
            if append:
                old = iface.CurrentValue or ""
                iface.SetValue(old + value)
            else:
                iface.SetValue(value)
            return {"ok": True,
                    "result": f'Set {ctype}: "{cname}" to "{iface.CurrentValue}"'}
        except Exception:
            pass
        try:
            if append:
                old = ctrl.get_value() if hasattr(ctrl, 'get_value') else ""
                ctrl.set_edit_text(old + value)
            else:
                ctrl.set_edit_text(value)
            return {"ok": True,
                    "result": f'Set {ctype}: "{cname}" to "{ctrl.window_text()}"'}
        except Exception as e:
            return {"ok": False,
                    "result": f'Cannot set text on {ctype} "{cname}": {e}'}

    def _cmd_find(self, req):
        name = req.get("name", "")
        ct = req.get("type", "")
        if not name:
            return {"ok": False, "result": "find requires a name"}
        if req.get("refresh"):
            self._cache = None
        _, all_matches = self.find_control(name, ct, index=0)
        if not all_matches:
            return {"ok": False,
                    "result": f"No controls found: name='{name}' type='{ct}'"}
        lines = [f"Found {len(all_matches)} match(es) for '{name}'"
                 + (f" type='{ct}'" if ct else "") + ":"]
        for idx, (ctrl, path, wt, wz) in enumerate(all_matches):
            ei = ctrl.element_info
            r = ei.rectangle
            wlabel = f'window="{wt}"' if wt else 'window=(main)'
            lines.append(
                f"  [{idx}] {wlabel} z={wz} "
                f"rect=({r.left},{r.top},{r.right},{r.bottom})")
            lines.append(f"        path: {path}")
        if len(all_matches) > 1:
            lines.append("")
            lines.append("Tip: --index N selects a specific match.")
        return {"ok": True, "result": "\n".join(lines)}

    def _cmd_alias(self, req):
        display = req.get("display", "").strip()
        uia_name = req.get("uia_name", "").strip()
        if not display or not uia_name:
            return {"ok": False, "result": "Need 'display' and 'uia_name'"}
        self._manual_aliases[display.lower()] = uia_name
        return {"ok": True,
                "result": f"Alias registered: '{display}' -> '{uia_name}'"}

    def _cmd_stop(self, req):
        return {"ok": True, "result": "Server stopping", "_stop": True}

    def _post_right_click(self, ctrl):
        """Open a control's context menu via SendInput at its center."""
        ei = ctrl.element_info
        r = ei.rectangle
        mid_x = (r.left + r.right) // 2
        mid_y = (r.top + r.bottom) // 2
        user32 = ctypes.windll.user32
        main_hwnd = self.win.handle
        target_tid, _ = win32process.GetWindowThreadProcessId(main_hwnd)
        my_tid = win32api.GetCurrentThreadId()
        user32.AttachThreadInput(my_tid, target_tid, True)
        try:
            user32.SetForegroundWindow(main_hwnd)
            time.sleep(0.1)
            user32.SetCursorPos(mid_x, mid_y)
            time.sleep(0.1)

            INPUT_MOUSE = 0

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ('dx', ctypes.c_long), ('dy', ctypes.c_long),
                    ('mouseData', ctypes.wintypes.DWORD),
                    ('dwFlags', ctypes.wintypes.DWORD),
                    ('time', ctypes.wintypes.DWORD),
                    ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUT(ctypes.Structure):
                class _U(ctypes.Union):
                    _fields_ = [('mi', MOUSEINPUT)]
                _fields_ = [
                    ('type', ctypes.wintypes.DWORD), ('ii', _U),
                ]

            def _send(flags):
                inp = INPUT()
                inp.type = INPUT_MOUSE
                inp.ii.mi.dwFlags = flags
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

            _send(0x0002)  # LEFTDOWN
            time.sleep(0.03)
            _send(0x0004)  # LEFTUP
            time.sleep(0.3)
            _send(0x0008)  # RIGHTDOWN
            time.sleep(0.05)
            _send(0x0010)  # RIGHTUP
        finally:
            user32.AttachThreadInput(my_tid, target_tid, False)

    def _cmd_right_click(self, req):
        name = req.get("name", "")
        ct = req.get("type", "")
        index = req.get("index", 0)
        self.ensure_connected()
        ctrl, all_matches = self.find_control(name, ct, index=index)
        if not ctrl:
            return {"ok": False,
                    "result": f"Not found: name='{name}' type='{ct}'"}
        ei = ctrl.element_info
        r = ei.rectangle
        self._post_right_click(ctrl)
        time.sleep(0.5)
        self._cache = None  # context menu may be a new window
        result = (f'Right-clicked {ei.control_type}: "{ei.name or ""}" '
                  f'at ({r.left},{r.top},{r.right},{r.bottom})')
        result += self._format_match_warning(all_matches, name, index)
        return {"ok": True, "result": result}

    def _cmd_wait_for(self, req):
        name = req.get("name", "")
        ct = req.get("type", "")
        timeout = req.get("timeout", 10)
        interval = req.get("interval", 0.5)
        if not name:
            return {"ok": False, "result": "Need 'name' to wait for"}
        self.ensure_connected()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._cache = None
            ctrl, _ = self.find_control(name, ct)
            if ctrl is not None:
                ei = ctrl.element_info
                elapsed = timeout - (deadline - time.time())
                return {"ok": True,
                        "result": (f'Found {ei.control_type}: '
                                   f'"{ei.name or ""}" after {elapsed:.1f}s')}
            time.sleep(interval)
        return {"ok": False,
                "result": f"Timeout ({timeout}s): '{name}' did not appear"}

    def _cmd_send_keys(self, req):
        keys_text = req.get("keys", "")
        vk_name = req.get("vk", "")
        modifiers = req.get("modifiers", []) or []
        name = req.get("name", "")
        ct = req.get("type", "")
        self.ensure_connected()

        hwnd = None
        if name:
            ctrl, _ = self.find_control(name, ct)
            if ctrl:
                hwnd = ctrl.handle
        if not hwnd:
            hwnd = self.win.handle if self.win else None
        if not hwnd:
            return {"ok": False, "result": "No target window found"}

        _VK_MAP = {
            "RETURN": win32con.VK_RETURN, "ENTER": win32con.VK_RETURN,
            "ESCAPE": win32con.VK_ESCAPE, "ESC": win32con.VK_ESCAPE,
            "TAB": win32con.VK_TAB,
            "DELETE": win32con.VK_DELETE, "DEL": win32con.VK_DELETE,
            "BACK": win32con.VK_BACK, "BACKSPACE": win32con.VK_BACK,
            "SPACE": win32con.VK_SPACE,
            "UP": win32con.VK_UP, "DOWN": win32con.VK_DOWN,
            "LEFT": win32con.VK_LEFT, "RIGHT": win32con.VK_RIGHT,
            "HOME": win32con.VK_HOME, "END": win32con.VK_END,
            "PAGEUP": win32con.VK_PRIOR, "PAGEDOWN": win32con.VK_NEXT,
            "F1": win32con.VK_F1, "F2": win32con.VK_F2,
            "F3": win32con.VK_F3, "F4": win32con.VK_F4,
            "F5": win32con.VK_F5, "F6": win32con.VK_F6,
            "F7": win32con.VK_F7, "F8": win32con.VK_F8,
            "F9": win32con.VK_F9, "F10": win32con.VK_F10,
            "F11": win32con.VK_F11, "F12": win32con.VK_F12,
        }
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            _VK_MAP[c] = ord(c)

        result_parts = []

        if keys_text:
            for ch in keys_text:
                win32gui.PostMessage(hwnd, win32con.WM_CHAR, ord(ch), 0)
                time.sleep(0.01)
            result_parts.append(f"Typed {len(keys_text)} chars")

        if vk_name:
            if isinstance(vk_name, str):
                vk = _VK_MAP.get(vk_name.upper())
                if vk is None:
                    try:
                        vk = int(vk_name, 0)
                    except ValueError:
                        return {"ok": False,
                                "result": f"Unknown VK: '{vk_name}'"}
            else:
                vk = int(vk_name)
            mod_vks = []
            for mod in modifiers:
                u = mod.upper()
                if u in ("CTRL", "CONTROL"):
                    mod_vks.append(win32con.VK_CONTROL)
                elif u == "SHIFT":
                    mod_vks.append(win32con.VK_SHIFT)
                elif u in ("ALT", "MENU"):
                    mod_vks.append(win32con.VK_MENU)
            for mvk in mod_vks:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, mvk, 0)
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
            time.sleep(0.05)
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
            for mvk in reversed(mod_vks):
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, mvk, 0)
            mod_str = "+".join(m.upper() for m in modifiers)
            key_str = (vk_name.upper() if isinstance(vk_name, str)
                       else f"0x{vk:02X}")
            result_parts.append(f"Sent {mod_str + '+' if mod_str else ''}{key_str}")

        if not result_parts:
            return {"ok": False,
                    "result": "Need 'keys' (text) or 'vk' (virtual key)"}
        return {"ok": True, "result": ", ".join(result_parts)}

    def _cmd_scroll(self, req):
        name = req.get("name", "")
        ct = req.get("type", "")
        direction = req.get("direction", "down").lower()
        amount = req.get("amount", 3)
        self.ensure_connected()

        if name:
            ctrl, _ = self.find_control(name, ct)
            if not ctrl:
                return {"ok": False,
                        "result": f"Not found: name='{name}' type='{ct}'"}
        else:
            ctrl = self.win

        try:
            iface = ctrl.iface_scroll
            for _ in range(amount):
                if direction == "down":
                    iface.Scroll(-1, 0)
                elif direction == "up":
                    iface.Scroll(-1, 1)
                elif direction == "right":
                    iface.Scroll(0, -1)
                elif direction == "left":
                    iface.Scroll(1, -1)
            ei = ctrl.element_info
            return {"ok": True,
                    "result": (f'Scrolled "{ei.name or "(unnamed)"}" '
                               f'{direction} x{amount} via UIA ScrollPattern')}
        except Exception:
            pass

        ei = ctrl.element_info
        r = ei.rectangle
        mid_x = (r.left + r.right) // 2
        mid_y = (r.top + r.bottom) // 2
        hwnd = ctrl.handle
        if not hwnd:
            parent = ctrl.parent()
            hwnd = parent.handle if parent else None
        if not hwnd:
            hwnd = self.win.handle
        if not hwnd:
            return {"ok": False, "result": "No HWND found for scroll target"}

        WHEEL_DELTA = 120
        if direction in ("up", "down"):
            msg = win32con.WM_MOUSEWHEEL
            delta = WHEEL_DELTA if direction == "up" else -WHEEL_DELTA
        elif direction in ("left", "right"):
            msg = 0x020E  # WM_MOUSEHWHEEL
            delta = -WHEEL_DELTA if direction == "left" else WHEEL_DELTA
        else:
            return {"ok": False,
                    "result": f"Unknown direction: '{direction}'"}
        screen_lParam = (mid_y << 16) | (mid_x & 0xFFFF)
        for _ in range(amount):
            wParam = (delta & 0xFFFF) << 16
            win32gui.PostMessage(hwnd, msg, wParam, screen_lParam)
            time.sleep(0.05)
        return {"ok": True,
                "result": (f'Scrolled "{ei.name or "(unnamed)"}" '
                           f'{direction} x{amount} via mouse wheel')}

    def _cmd_navigate(self, req):
        steps = req.get("steps", [])
        path_str = req.get("path", "")
        screenshot_after = req.get("screenshot", False)
        screenshot_path = req.get("screenshot_path", "")
        refresh_between = req.get("refresh", True)

        if path_str and not steps:
            steps = []
            for part in path_str.split(">"):
                part = part.strip()
                if not part:
                    continue
                if ":" in part:
                    ct, name = part.split(":", 1)
                    steps.append({"name": name.strip(),
                                  "type": ct.strip()})
                else:
                    steps.append({"name": part})

        if not steps:
            return {"ok": False,
                    "result": "No steps. Use 'path' or 'steps'."}

        self.ensure_connected()
        log = []
        for i, step in enumerate(steps):
            name = step.get("name", "")
            ct = step.get("type", "")
            index = step.get("index", 0)
            if not name:
                log.append(f"Step {i+1}: SKIP (empty name)")
                continue
            if refresh_between and i > 0:
                self._cache = None
            ctrl, all_matches = self.find_control(name, ct, index=index)
            if not ctrl:
                self._cache = None
                ctrl, all_matches = self.find_control(name, ct, index=index)
                if not ctrl:
                    log.append(
                        f"Step {i+1}: FAIL not found name='{name}' type='{ct}'")
                    return {"ok": False, "result": "\n".join(log),
                            "failed_step": i + 1, "steps_completed": i}
            ei = ctrl.element_info
            ctype = ei.control_type
            cname = ei.name or ""
            cm = self._click_control(ctrl)
            time.sleep(0.3)
            state = self._get_control_state(ctrl, ctype)
            log.append(f'Step {i+1}: OK {ctype}: "{cname}"{state} via {cm}')

        if screenshot_after:
            ss_req = {"path": screenshot_path} if screenshot_path else {}
            ss = self._cmd_screenshot(ss_req)
            log.append(f"Screenshot: {ss.get('result', 'failed')}")

        return {"ok": True, "result": "\n".join(log),
                "steps_completed": len(steps)}

    def _cmd_batch(self, req):
        commands = req.get("commands", [])
        continue_on_error = req.get("continue_on_error", False)
        if not commands:
            return {"ok": False, "result": "No commands provided."}
        results = []
        for i, cr in enumerate(commands):
            cn = cr.get("cmd", "")
            if cn in ("stop", "batch", "navigate"):
                results.append({"ok": False,
                                "result": f"Cannot nest '{cn}' in batch"})
                if not continue_on_error:
                    return {"ok": False, "results": results,
                            "result": f"Failed at command {i+1}: nested '{cn}'",
                            "failed_at": i}
                continue
            resp = self.handle(cr)
            results.append(resp)
            if not resp.get("ok") and not continue_on_error:
                return {"ok": False, "results": results,
                        "result": (f"Failed at command {i+1}: "
                                   f"{resp.get('result', '')}"),
                        "failed_at": i}
        ok_count = sum(1 for r in results if r.get("ok"))
        combined = []
        for i, r in enumerate(results):
            status = "OK" if r.get("ok") else "FAIL"
            combined.append(f"[{i+1}] {status}: {r.get('result', '')}")
        return {"ok": ok_count == len(results),
                "result": (f"Batch complete: {ok_count}/{len(results)} "
                           f"succeeded\n" + "\n".join(combined)),
                "results": results}

    # ---- Server loop ----------------------------------------------------

    def run(self):
        self._log(f"Starting GUI Nav Server (target={self.target_process}, "
                  f"port={self.port})...")
        self._start_session_monitor()
        try:
            self.ensure_connected()
            self._log(f"Connected to {self.target_process}")
            self.get_cache()
            self._log(f"Ready on {HOST}:{self.port} "
                      f"({len(self._cache)} controls cached)")
        except Exception as e:
            self._log(f"Warning: initial connection failed ({e}). "
                      "Will retry on first command.")

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, self.port))
        srv.listen(5)
        srv.settimeout(10)

        running = True
        while running:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                if time.time() - self._last_activity > IDLE_TIMEOUT:
                    self._log("Idle timeout, shutting down")
                    break
                continue
            except OSError:
                break

            try:
                data = b""
                conn.settimeout(120)
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    req = json.loads(data.decode("utf-8").strip())
                    self._log(f"<- {req.get('cmd', '?')}")
                    with self._lock:
                        resp = self.handle(req)
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    self._log(f"-> ok={resp.get('ok')}")
                    if resp.get("_stop"):
                        running = False
            except Exception as e:
                self._log(f"Error: {e}")
                try:
                    err = json.dumps({"ok": False, "result": str(e)}) + "\n"
                    conn.sendall(err.encode("utf-8"))
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        srv.close()
        self._log("Server stopped")


# ===========================================================================
#  RDP keep-alive scheduled task installer
# ===========================================================================

def _prepare_rdp(task_name="GUINavReconnect"):
    """Install a SYSTEM scheduled task that reconnects the user's session
    to the physical console whenever RDP disconnects (Event ID 24).

    Generic: no application paths or process names are baked into the task.
    Run once from an elevated prompt."""
    if sys.platform != "win32":
        print("ERROR: prepare-rdp is Windows-only.", file=sys.stderr)
        return False
    if not _HAS_CTYPES or not ctypes.windll.shell32.IsUserAnAdmin():
        print("ERROR: must be run from an elevated (admin) prompt.",
              file=sys.stderr)
        return False

    sid = ctypes.c_ulong()
    ctypes.WinDLL("kernel32").ProcessIdToSessionId(os.getpid(),
                                                   ctypes.byref(sid))
    sid = sid.value
    work_dir = Path.cwd()
    reconnect_script = work_dir / "gui_nav_reconnect_session.ps1"
    session_file = work_dir / "gui_nav_session_id.txt"
    task_xml_file = work_dir / "gui_nav_reconnect_task.xml"

    session_file.write_text(str(sid), encoding="utf-8")
    print(f"Session ID {sid} written to {session_file}")

    ps_content = (
        "# Auto-generated by gui_nav.py prepare-rdp\n"
        f'$sid = (Get-Content "{session_file}" -ErrorAction Stop).Trim()\n'
        "$q = query session $sid 2>&1 | Out-String\n"
        "if ($q -match 'Disc') { & tscon $sid /dest:console }\n"
    )
    reconnect_script.write_text(ps_content, encoding="utf-8")

    task_xml = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <Triggers>\n'
        '    <EventTrigger>\n'
        '      <Enabled>true</Enabled>\n'
        '      <Subscription>'
        '&lt;QueryList&gt;'
        '&lt;Query Id="0" Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"&gt;'
        '&lt;Select Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"&gt;'
        '*[System[EventID=24]]'
        '&lt;/Select&gt;'
        '&lt;/Query&gt;'
        '&lt;/QueryList&gt;'
        '</Subscription>\n'
        '    </EventTrigger>\n'
        '  </Triggers>\n'
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        '      <UserId>S-1-5-18</UserId>\n'
        '      <RunLevel>HighestAvailable</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <ExecutionTimeLimit>PT30S</ExecutionTimeLimit>\n'
        '  </Settings>\n'
        '  <Actions>\n'
        '    <Exec>\n'
        '      <Command>powershell.exe</Command>\n'
        f'      <Arguments>-ExecutionPolicy Bypass -File "{reconnect_script}"</Arguments>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    )
    task_xml_file.write_text(task_xml, encoding="utf-16")

    result = subprocess.run(
        ["schtasks", "/create", "/tn", task_name,
         "/xml", str(task_xml_file), "/f"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Scheduled task '{task_name}' created.")
        print(f"On RDP disconnect, session {sid} will move to the console.")
        return True
    print(f"WARNING: Could not create scheduled task: {result.stderr.strip()}",
          file=sys.stderr)
    return False


# ===========================================================================
#  Client (TCP wire) + nav_* helpers
# ===========================================================================

# These globals are populated by main() (or by a caller of _set_endpoint)
# so the client functions know where to talk and which log to tail.
_CLIENT_PORT = DEFAULT_PORT
_CLIENT_LOG = Path(DEFAULT_LOG_FILE).resolve()
_CLIENT_PROCESS = None  # only needed when auto-spawning the server


def _set_endpoint(port=None, log_file=None, target_process=None):
    """Override client globals (port / log / target). Useful when importing
    nav_* helpers from other Python code without going through the CLI."""
    global _CLIENT_PORT, _CLIENT_LOG, _CLIENT_PROCESS
    if port is not None:
        _CLIENT_PORT = int(port)
    if log_file is not None:
        _CLIENT_LOG = Path(log_file).resolve()
    if target_process is not None:
        _CLIENT_PROCESS = target_process


def _send(cmd_dict, timeout=120):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((HOST, _CLIENT_PORT))
        sock.sendall((json.dumps(cmd_dict) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8").strip())
    except ConnectionRefusedError:
        return {"ok": False,
                "result": "GUI Nav server not running."}
    except socket.timeout:
        return {"ok": False,
                "result": "GUI Nav server timed out."}
    except Exception as e:
        return {"ok": False, "result": f"GUI Nav connection error: {e}"}
    finally:
        sock.close()


def _ensure_server():
    """Ensure server is running. Auto-restarts when this script is newer
    than the currently-running server, so code edits get picked up."""
    resp = _send({"cmd": "ping"}, timeout=3)
    if resp.get("ok"):
        try:
            mtime = Path(__file__).resolve().stat().st_mtime
        except Exception:
            return True
        srv_start = resp.get("start_time")
        if srv_start is not None and mtime <= srv_start:
            return True
        print("gui_nav.py changed since server started, restarting...",
              file=sys.stderr)
        _send({"cmd": "stop"}, timeout=3)
        time.sleep(0.5)

    target = _CLIENT_PROCESS or _resolve_target_process(None)
    if not target:
        print("ERROR: target_process is not configured. Pass --process "
              "or set 'target_process:' in CoderAgentConfig.yaml.",
              file=sys.stderr)
        return False

    script = str(Path(__file__).resolve())
    log_path = str(_CLIENT_LOG)
    log_fh = open(log_path, "a")
    args = [sys.executable, script, "serve",
            "--process", target,
            "--port", str(_CLIENT_PORT),
            "--log", log_path]
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(args, creationflags=CREATE_NO_WINDOW,
                         stdout=log_fh, stderr=log_fh)
    else:
        subprocess.Popen(args, start_new_session=True, close_fds=True,
                         stdout=log_fh, stderr=log_fh)

    print("Starting GUI Nav server (first-time cache build ~25s)...",
          file=sys.stderr)
    for _ in range(45):
        time.sleep(1)
        if _send({"cmd": "ping"}, timeout=3).get("ok"):
            print("Server ready.", file=sys.stderr)
            return True
    print(f"Error: Server did not start. Check {log_path}", file=sys.stderr)
    return False


def _call(cmd_dict, timeout=120):
    if not _ensure_server():
        return "Error: GUI Nav server failed to start."
    return _send(cmd_dict, timeout=timeout).get("result", "No result")


# ---- Public nav_* helpers (importable) -------------------------------------

def nav_tree(name="", control_type="", refresh=False):
    return _call({"cmd": "tree", "name": name, "type": control_type,
                  "refresh": refresh})


def nav_click(name, control_type="", index=0, refresh=False, method="auto"):
    return _call({"cmd": "click", "name": name, "type": control_type,
                  "index": index, "refresh": refresh, "method": method})


def nav_inspect(name, control_type=""):
    return _call({"cmd": "inspect", "name": name, "type": control_type})


def nav_find(name, control_type="", refresh=False):
    return _call({"cmd": "find", "name": name, "type": control_type,
                  "refresh": refresh})


def nav_text(name="", control_type="", max_items=200):
    return _call({"cmd": "text", "name": name, "type": control_type,
                  "max": max_items})


def nav_refresh():
    return _call({"cmd": "refresh"})


def nav_status():
    return _call({"cmd": "status"})


def nav_stop():
    try:
        return _send({"cmd": "stop"}, timeout=5).get("result", "Stopped")
    except Exception:
        return "Server not running"


def nav_screenshot(save_path="", view_max=1280, no_view=False):
    return _call({"cmd": "screenshot", "path": save_path,
                  "view_max": view_max, "no_view": no_view})


def nav_set_text(name, value, control_type="", append=False):
    return _call({"cmd": "set_text", "name": name, "value": value,
                  "type": control_type, "append": append})


def nav_navigate(path="", steps=None, screenshot=False, screenshot_path=""):
    cmd = {"cmd": "navigate", "path": path, "screenshot": screenshot}
    if steps:
        cmd["steps"] = steps
    if screenshot_path:
        cmd["screenshot_path"] = screenshot_path
    return _call(cmd, timeout=120)


def nav_batch(commands):
    return _call({"cmd": "batch", "commands": commands}, timeout=120)


def nav_alias(display, uia_name):
    return _call({"cmd": "alias", "display": display, "uia_name": uia_name})


def nav_right_click(name, control_type="", index=0):
    return _call({"cmd": "right_click", "name": name, "type": control_type,
                  "index": index})


def nav_click_xy(x, y, right=False):
    return _call({"cmd": "click_xy", "x": x, "y": y, "right": right})


def nav_wait_for(name, control_type="", timeout=10, interval=0.5):
    return _call({"cmd": "wait_for", "name": name, "type": control_type,
                  "timeout": timeout, "interval": interval},
                 timeout=timeout + 5)


def nav_send_keys(keys="", vk="", modifiers=None, name="", control_type=""):
    cmd = {"cmd": "send_keys", "keys": keys, "vk": vk, "name": name,
           "type": control_type}
    if modifiers:
        cmd["modifiers"] = modifiers
    return _call(cmd)


def nav_scroll(name="", control_type="", direction="down", amount=3):
    return _call({"cmd": "scroll", "name": name, "type": control_type,
                  "direction": direction, "amount": amount})


# ===========================================================================
#  CLI
# ===========================================================================

def _build_parser():
    p = argparse.ArgumentParser(
        prog="gui_nav.py",
        description=("Generic Windows UIA automation server. Target process "
                     "is required via --process or CoderAgentConfig.yaml."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--process", default="",
                   help="Target process name (e.g. Notepad.exe). "
                        "Overrides target_process in CoderAgentConfig.yaml.")
    p.add_argument("--port", type=int, default=0,
                   help=f"TCP port (default: {DEFAULT_PORT}, or "
                        "gui_nav_port from config).")
    p.add_argument("--log", default=DEFAULT_LOG_FILE,
                   help=f"Server log file (default: {DEFAULT_LOG_FILE}).")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start server (foreground)")

    pt = sub.add_parser("tree", help="List controls")
    pt.add_argument("--name", default="")
    pt.add_argument("--type", default="", dest="control_type")
    pt.add_argument("--refresh", action="store_true")

    pc = sub.add_parser("click", help="Click a control by name")
    pc.add_argument("name")
    pc.add_argument("--type", default="", dest="control_type")
    pc.add_argument("--index", type=int, default=0)
    pc.add_argument("--refresh", action="store_true")
    pc.add_argument("--method", default="auto",
                    choices=["auto", "sendinput", "postmessage", "uia"])

    pi = sub.add_parser("inspect", help="Detailed control info")
    pi.add_argument("name")
    pi.add_argument("--type", default="", dest="control_type")

    pf = sub.add_parser("find", help="Find all matches (no click)")
    pf.add_argument("name")
    pf.add_argument("--type", default="", dest="control_type")
    pf.add_argument("--refresh", action="store_true")

    px = sub.add_parser("text", help="Dump text from control subtree")
    px.add_argument("name", nargs="?", default="")
    px.add_argument("--type", default="", dest="control_type")
    px.add_argument("--max", type=int, default=200, dest="max_items")

    sub.add_parser("refresh", help="Rebuild control cache")
    sub.add_parser("status", help="Server status")
    sub.add_parser("stop", help="Stop the server")

    pst = sub.add_parser("set-text", help="Set text on Edit/ComboBox")
    pst.add_argument("name")
    pst.add_argument("value")
    pst.add_argument("--type", default="", dest="control_type")
    pst.add_argument("--append", action="store_true")

    pss = sub.add_parser("screenshot", help="PrintWindow capture")
    pss.add_argument("--path", default="")
    pss.add_argument("--view-max", type=int, default=1280, dest="view_max")
    pss.add_argument("--no-view", action="store_true", dest="no_view")

    pn = sub.add_parser("navigate", help="Multi-step click path")
    pn.add_argument("path", help='e.g. "Tab1 > TabItem:Sub > Button:OK"')
    pn.add_argument("--screenshot", action="store_true")
    pn.add_argument("--screenshot-path", default="", dest="screenshot_path")

    pa = sub.add_parser("alias", help="Register display->UIA name alias")
    pa.add_argument("display")
    pa.add_argument("uia_name")

    prc = sub.add_parser("right-click", help="Right-click a control")
    prc.add_argument("name")
    prc.add_argument("--type", default="", dest="control_type")
    prc.add_argument("--index", type=int, default=0)

    pcxy = sub.add_parser("click-xy",
                          help="SendInput click at absolute (x, y)")
    pcxy.add_argument("x", type=int)
    pcxy.add_argument("y", type=int)
    pcxy.add_argument("--right", action="store_true")

    pwf = sub.add_parser("wait-for", help="Wait for a control to appear")
    pwf.add_argument("name")
    pwf.add_argument("--type", default="", dest="control_type")
    pwf.add_argument("--timeout", type=float, default=10)

    psk = sub.add_parser("send-keys", help="Send keyboard input")
    psk.add_argument("--keys", default="")
    psk.add_argument("--vk", default="")
    psk.add_argument("--mod", action="append", default=[], dest="modifiers")
    psk.add_argument("--name", default="")
    psk.add_argument("--type", default="", dest="control_type")

    pscr = sub.add_parser("scroll", help="Scroll a control")
    pscr.add_argument("--name", default="")
    pscr.add_argument("--type", default="", dest="control_type")
    pscr.add_argument("--direction", default="down",
                      choices=["up", "down", "left", "right"])
    pscr.add_argument("--amount", type=int, default=3)

    sub.add_parser("prepare-rdp",
                   help="One-time admin: install RDP-reconnect task")

    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    # Configure client endpoint from CLI / config (used by every subcommand).
    port = _resolve_port(args.port if args.port else None)
    log_file = Path(args.log).resolve()
    target = _resolve_target_process(args.process)
    _set_endpoint(port=port, log_file=log_file, target_process=target)

    if args.command == "serve":
        _require_windows_runtime()
        if not target:
            print("ERROR: target process is required. Pass --process or set "
                  "'target_process:' in CoderAgentConfig.yaml.",
                  file=sys.stderr)
            return 2
        GUINavServer(target_process=target, port=port,
                     log_file=str(log_file)).run()
        return 0

    if args.command == "prepare-rdp":
        return 0 if _prepare_rdp() else 2

    # All remaining client subcommands need a target so the server can be
    # auto-spawned if not running. The actual runtime deps are checked by
    # the spawned server process; the client itself only needs the stdlib.
    if args.command != "stop" and not target:
        print("ERROR: target process is required. Pass --process or set "
              "'target_process:' in CoderAgentConfig.yaml.",
              file=sys.stderr)
        return 2

    cmd = args.command
    if cmd == "tree":
        print(nav_tree(args.name, args.control_type, args.refresh))
    elif cmd == "click":
        print(nav_click(args.name, args.control_type, index=args.index,
                        refresh=args.refresh, method=args.method))
    elif cmd == "inspect":
        print(nav_inspect(args.name, args.control_type))
    elif cmd == "find":
        print(nav_find(args.name, args.control_type, args.refresh))
    elif cmd == "text":
        print(nav_text(args.name, args.control_type, args.max_items))
    elif cmd == "refresh":
        print(nav_refresh())
    elif cmd == "status":
        print(nav_status())
    elif cmd == "stop":
        print(nav_stop())
    elif cmd == "set-text":
        print(nav_set_text(args.name, args.value, args.control_type,
                           args.append))
    elif cmd == "screenshot":
        print(nav_screenshot(args.path, view_max=args.view_max,
                             no_view=args.no_view))
    elif cmd == "navigate":
        print(nav_navigate(path=args.path, screenshot=args.screenshot,
                           screenshot_path=args.screenshot_path))
    elif cmd == "alias":
        print(nav_alias(args.display, args.uia_name))
    elif cmd == "right-click":
        print(nav_right_click(args.name, args.control_type, args.index))
    elif cmd == "click-xy":
        print(nav_click_xy(args.x, args.y, right=args.right))
    elif cmd == "wait-for":
        print(nav_wait_for(args.name, args.control_type, args.timeout))
    elif cmd == "send-keys":
        print(nav_send_keys(keys=args.keys, vk=args.vk,
                            modifiers=args.modifiers, name=args.name,
                            control_type=args.control_type))
    elif cmd == "scroll":
        print(nav_scroll(args.name, args.control_type, args.direction,
                         args.amount))
    return 0


if __name__ == "__main__":
    sys.exit(main())
