"""RiftLens desktop app: the web app in a native window, plus the client assistant.

    .venv\\Scripts\\python desktop.py            # from source
    RiftLens.exe                                # after `python build_desktop.py`

The server runs on a free localhost port inside this process and the window
opens on the client assistant. Closing the window quits the app.

The Tencent (国服) client runs elevated; run RiftLens as administrator there
or it cannot read the client's connection details.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BRIDGE_CLI = len(sys.argv) > 1 and sys.argv[1] == "bridge"  # Claude Code calling the review tools through the exe


def _attach_std_handles() -> None:
    """A windowed exe starts with sys.stdin/stdout None even when its caller passed pipes; reattach them."""
    import ctypes
    import msvcrt

    k32 = ctypes.windll.kernel32
    for num, name, mode in ((-10, "stdin", "r"), (-11, "stdout", "w"), (-12, "stderr", "w")):
        h = k32.GetStdHandle(num)
        if h in (0, -1, None):
            continue
        try:
            fd = msvcrt.open_osfhandle(h, os.O_RDONLY if mode == "r" else os.O_WRONLY)
            setattr(sys, name, open(fd, mode, encoding="utf-8", errors="replace", newline="\n"))
        except OSError:
            pass


if FROZEN and BRIDGE_CLI:
    _attach_std_handles()
    os.environ.setdefault("RIFTLENS_STATE_DIR", str(Path(os.environ.get("APPDATA") or Path.home()) / "RiftLens"))
    os.environ.setdefault("RIFTLENS_CACHE_DIR", str(Path(os.environ["RIFTLENS_STATE_DIR"]) / "cache"))
    os.environ["RIFTLENS_CLIENT_ASSISTANT"] = "0"
elif FROZEN:  # keep user data out of the install folder
    home = Path(os.environ.get("APPDATA") or Path.home()) / "RiftLens"
    os.environ.setdefault("RIFTLENS_STATE_DIR", str(home))
    os.environ.setdefault("RIFTLENS_CACHE_DIR", str(home / "cache"))
    home.mkdir(parents=True, exist_ok=True)
    os.chdir(home)
    # A windowed exe has no console: sys.stdout/stderr are None, and anything that prints would raise.
    # Send all output and tracebacks to a log file instead so crashes can be diagnosed.
    _log = open(home / "riftlens.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
else:
    os.chdir(Path(__file__).resolve().parent)


def _excepthook(kind, exc, tb):
    import traceback

    sys.stderr.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + "".join(traceback.format_exception(kind, exc, tb)))
    sys.stderr.flush()


sys.excepthook = _excepthook
threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int) -> None:
    import uvicorn

    from web.app import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=None, access_log=False)


def wait_up(port: int, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def dark_title_bar(window) -> None:
    """Paint the native Windows title bar and border to match the dark page (Windows 10 1809+ / 11)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = int(str(window.native.Handle.ToInt64()))
        dwm = ctypes.windll.dwmapi

        def put(attr: int, value: int) -> None:
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        put(20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10: dark caption)
        put(35, 0x00111108)  # DWMWA_CAPTION_COLOR = #081111 (Windows 11), COLORREF is 0x00BBGGRR
        put(34, 0x00302F21)  # DWMWA_BORDER_COLOR  = #212f30
        put(36, 0x00E8EBE2)  # DWMWA_TEXT_COLOR    = #e2ebe8
    except Exception as e:  # cosmetic only
        print("dark title bar failed:", e)


def start_agent() -> None:
    """Without an LLM configured, answer with the user's own Claude Code (subscription) when it is installed."""
    from lol_review.riot import load_env

    load_env()
    if os.environ.get("AGENT_MODE") or all(os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")):
        return  # explicitly configured
    from web import headless_worker

    if headless_worker.claude_path():
        os.environ["AGENT_MODE_FALLBACK"] = "bridge"
        headless_worker.start_background(os.environ.get("CLAUDE_MODEL") or None)
        print(time.strftime("[%Y-%m-%d %H:%M:%S]"), "AI 复盘：使用本机 Claude Code", headless_worker.claude_path())
    else:
        os.environ["AGENT_MODE_FALLBACK"] = "b0"
        print(time.strftime("[%Y-%m-%d %H:%M:%S]"), "AI 复盘：没有找到 Claude Code，使用无模型模式")


IN_GAME = ("GameStart", "InProgress", "Reconnect")


class Overlay:
    """The in-game scouting window: frameless, always on top, toggled with Shift+Tab while a game is running.

    The hotkey is registered only during a game so Shift+Tab keeps working everywhere else. It needs the game
    in borderless or windowed mode; nothing can draw over exclusive fullscreen.
    """

    def __init__(self, window) -> None:
        self.window = window
        self.visible = False

    def toggle(self) -> bool:
        self.set(not self.visible)
        return self.visible

    def set(self, on: bool) -> None:
        if on == self.visible:
            return
        self.visible = on
        (self.window.show if on else self.window.hide)()

    def run_hotkey(self) -> None:
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        from lol_review import assistant

        user32 = ctypes.windll.user32
        MOD_SHIFT, MOD_NOREPEAT, VK_TAB, WM_HOTKEY, PM_REMOVE, HOTKEY_ID = 0x4, 0x4000, 0x09, 0x0312, 0x1, 0x524C
        msg = wintypes.MSG()
        registered, was_in_game = False, False
        while True:
            a = assistant.get()
            in_game = a.state.get("phase") in IN_GAME
            want = in_game and a.settings.get("overlay_hotkey", True)
            if want and not registered:
                registered = bool(user32.RegisterHotKey(None, HOTKEY_ID, MOD_SHIFT | MOD_NOREPEAT, VK_TAB))
                if not registered:
                    print("Shift+Tab 已被其他程序占用，悬浮窗热键不可用")
            elif not want and registered:
                user32.UnregisterHotKey(None, HOTKEY_ID)
                registered = False
            if in_game and not was_in_game and a.settings.get("overlay_auto"):
                self.set(True)
            if was_in_game and not in_game:
                self.set(False)
            was_in_game = in_game
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.toggle()
            time.sleep(0.05)


def main() -> None:
    import webview

    start_agent()
    port = int(os.environ.get("RIFTLENS_PORT") or free_port())
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), f"RiftLens 启动，端口 {port}")
    if not wait_up(port):
        raise SystemExit("RiftLens 服务启动失败，详情见 riftlens.log")
    window = webview.create_window("RiftLens", f"http://127.0.0.1:{port}/?desktop=1#client", width=1320, height=860,
                                   min_size=(900, 600), background_color="#0b1414")
    window.events.shown += lambda: dark_title_bar(window)
    ov_window = webview.create_window("RiftLens 悬浮窗", f"http://127.0.0.1:{port}/overlay", width=1180, height=540, x=40, y=40,
                                      frameless=True, easy_drag=True, on_top=True, hidden=True, focus=False,
                                      background_color="#0d1214")
    overlay = Overlay(ov_window)
    from web import app as web_app

    web_app.OVERLAY["toggle"] = overlay.toggle
    window.events.closed += lambda: os._exit(0)  # closing the main window quits, hidden overlay included
    threading.Thread(target=overlay.run_hotkey, name="overlay-hotkey", daemon=True).start()
    state = Path(os.environ.get("RIFTLENS_STATE_DIR") or Path.cwd())
    # keep localStorage (theme, rail, last reviewed game) between launches; pywebview defaults to a private session
    webview.start(private_mode=False, storage_path=str(state / "webview"))


if __name__ == "__main__":
    if BRIDGE_CLI:
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from web import bridge

        bridge._cli()
        sys.stdout.flush()
    else:
        main()
