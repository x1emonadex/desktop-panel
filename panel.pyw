# -*- coding: utf-8 -*-
"""
Панель — невидимая кнопка внизу по центру экрана.
Клик по тонкой полоске -> выезжает панель с ярлыками из папки.
Правый клик по полоске -> меню (папка, настройки, выход).
"""
import ctypes
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from tkinter import font as tkfont

try:
    from PIL import Image, ImageTk
except ImportError:  # понятная ошибка вместо тихого падения pythonw
    ctypes.windll.user32.MessageBoxW(
        None,
        "Не найден Pillow.\n\nУстановите его командой:\n    pip install pillow",
        "Панель", 0x10)
    raise

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")
ICON_CACHE_DIR = os.path.join(APP_DIR, "icons")
LOG_PATH = os.path.join(APP_DIR, "panel.log")
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup\Панель.lnk")
MUTEX_HANDLE = None

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# Явные прототипы: иначе ctypes режет 64-битные lParam/HWND до int32
# и в оконной процедуре трея падает OverflowError.
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]

KEY = "#010203"  # цвет-ключ прозрачности (не должен встречаться в интерфейсе)

DEFAULTS = {
    "folder": os.path.join(os.path.expanduser("~"), "Панель"),
    "columns": 6,
    "tile_width": 112,
    "tile_height": 108,
    "gap": 8,
    "padding": 16,
    "max_rows": 4,
    "radius": 20,
    "accent": "#D8D8E0",
    "trigger_width": 420,
    "trigger_height": 14,
    "trigger_alpha": 0.10,
    "trigger_alpha_hover": 0.45,
    "trigger_fill": "#1B1B21",
    "trigger_fill_hover": "#2E2E37",
    "panel_alpha": 0.97,
    "panel_color": "#16161A",
    "tile_color": "#1E1E23",
    "tile_hover": "#2B2B33",
    "border_color": "#34343C",
    "text_color": "#E7E7EC",
    "text_dim": "#8E8E99",
    "font": "Segoe UI",
    "font_size": 9,
    "icon_size": 44,
    "hotkey": "ctrl+alt+space",
}


def log(msg):
    try:
        # не даём логу расти бесконечно
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200 * 1024:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-300:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------- config
def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log("config error: %r" % (e,))
    else:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return cfg


CFG = load_config()


# ---------------------------------------------------------------- state
def load_state():
    st = {"pinned": [], "order": [], "view": "auto"}
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st.update(json.load(f))
    except Exception as e:
        log("state load: %r" % (e,))
    return st


def save_state(st):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("state save: %r" % (e,))


# ---------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE = "Панель"


def _autostart_cmd():
    """Команда для автозапуска: Panel.exe, если он есть, иначе pythonw + panel.pyw."""
    exe = os.path.join(APP_DIR, "Panel.exe")
    if os.path.exists(exe):
        return '"%s"' % exe
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.exists(pyw):
        return '"%s" "%s"' % (pyw, os.path.abspath(__file__))
    return None


def autostart_state():
    """True, если автозапуск включён и не отключён в диспетчере задач."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
    except FileNotFoundError:
        return False
    except Exception as e:
        log("autostart read: %r" % (e,))
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            data, _t = winreg.QueryValueEx(k, RUN_VALUE)
            if isinstance(data, (bytes, bytearray)) and len(data) >= 1 and data[0] in (2, 3):
                return data[0] == 2
    except FileNotFoundError:
        pass
    except Exception as e:
        log("autostart state: %r" % (e,))
    return True


def set_autostart(want):
    try:
        if want:
            cmd = _autostart_cmd()
            if not cmd:
                log("autostart: не найден Panel.exe/pythonw")
                return False
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
            try:  # снять «отключено» из диспетчера задач
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, RUN_VALUE)
            except Exception:
                pass
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
        # старый ярлык из Автозагрузки больше не нужен
        try:
            if os.path.exists(STARTUP_LNK):
                os.remove(STARTUP_LNK)
        except Exception:
            pass
        log("autostart -> %s" % want)
        return True
    except Exception as e:
        log("autostart set: %r" % (e,))
        return False


# ---------------------------------------------------------------- dpi
def setup_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def screen_metrics():
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    wa = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
    if wa.right <= 0 or wa.bottom <= 0:
        return w, h, 0, 0, w, h
    return w, h, wa.left, wa.top, wa.right, wa.bottom


# ---------------------------------------------------------------- icons
SHGFI_SYSICONINDEX = 0x4000
SHGFI_USEFILEATTRIBUTES = 0x10
SHIL_EXTRALARGE = 2
DI_NORMAL = 3


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON),
                ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260),
                ("szTypeName", wintypes.WCHAR * 80)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


IID_IImageList = GUID(0x46EB5926, 0x582E, 0x4017,
                      (ctypes.c_ubyte * 8)(0x9F, 0xDF, 0xE8, 0x99, 0x8D, 0xAA, 0x09, 0x50))


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


class ShellIcons:
    def __init__(self):
        self.iml = None
        self.get_icon = None
        try:
            piml = ctypes.c_void_p()
            hr = shell32[727](SHIL_EXTRALARGE, ctypes.byref(IID_IImageList), ctypes.byref(piml))
            if hr == 0 and piml.value:
                self.iml = piml
                vt = ctypes.cast(piml, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                self.get_icon = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_int,
                    ctypes.c_uint, ctypes.POINTER(wintypes.HICON))(vt[10])
        except Exception as e:
            log("shell icons init: %r" % (e,))

    def _hicon_to_image(self, hicon, size):
        hdc = user32.GetDC(0)
        memdc = gdi32.CreateCompatibleDC(hdc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        ppv = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), 0, ctypes.byref(ppv), None, 0)
        old = gdi32.SelectObject(memdc, hbmp)
        ctypes.memset(ppv, 0, size * size * 4)
        user32.DrawIconEx(memdc, 0, 0, ctypes.c_void_p(hicon), size, size, 0, None, DI_NORMAL)
        buf = ctypes.string_at(ppv, size * size * 4)
        img = Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1).copy()
        gdi32.SelectObject(memdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdc)
        if img.getchannel("A").getextrema()[1] == 0:
            img.putalpha(255)
        return img

    def image(self, path, size=48):
        if not self.get_icon:
            return None
        try:
            info = SHFILEINFOW()
            flags = SHGFI_SYSICONINDEX
            if not os.path.exists(path):
                flags |= SHGFI_USEFILEATTRIBUTES
            shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0, ctypes.byref(info),
                                   ctypes.sizeof(info), flags)
            ph = wintypes.HICON()
            hr = self.get_icon(self.iml, info.iIcon, 1, ctypes.byref(ph))
            if hr != 0 or not ph.value:
                return None
            img = self._hicon_to_image(ph.value, size)
            user32.DestroyIcon(ph)
            return img
        except Exception as e:
            log("icon %s: %r" % (path, e))
            return None


ICONS = ShellIcons()


def cleanup_icon_cache(limit=400, max_age_days=7):
    """Кэш иконок не должен расти бесконечно: старые файлы удаляем."""
    try:
        if not os.path.isdir(ICON_CACHE_DIR):
            return
        now = time.time()
        files = [os.path.join(ICON_CACHE_DIR, f) for f in os.listdir(ICON_CACHE_DIR)
                 if f.lower().endswith(".png")]
        # давно не использовавшиеся иконки
        for p in files:
            try:
                if now - os.path.getmtime(p) > max_age_days * 86400:
                    os.remove(p)
            except Exception:
                pass
        files = [p for p in files if os.path.exists(p)]
        if len(files) <= limit:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:len(files) - limit]:
            try:
                os.remove(p)
            except Exception:
                pass
    except Exception as e:
        log("icon cache cleanup: %r" % (e,))


def icon_file(path, size):
    """Кэшированная PNG-иконка для пути. Возвращает путь к файлу или None."""
    try:
        st = os.stat(path)
        stamp = "%s|%s|%s|%s" % (path, st.st_mtime, st.st_size, size)
    except OSError:
        stamp = "%s|%s" % (path, size)
    h = hashlib.md5(stamp.encode("utf-8", "ignore")).hexdigest()
    cache = os.path.join(ICON_CACHE_DIR, h + ".png")
    if os.path.exists(cache):
        try:
            os.utime(cache, None)   # отмечаем, что иконка ещё нужна
        except Exception:
            pass
        return cache
    img = ICONS.image(path, size)
    if img is None:
        return None
    try:
        os.makedirs(ICON_CACHE_DIR, exist_ok=True)
        img.save(cache)
    except Exception:
        return None
    return cache


# ---------------------------------------------------------------- helpers
def round_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def short_label(name):
    base, ext = os.path.splitext(name)
    if ext.lower() in (".lnk", ".url", ".appref-ms"):
        return base
    return name


def cursor_pos():
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def left_button_down():
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


def apply_window_icon(window):
    """Принудительно повесить иконку «Панели» на окно (иначе в панели задач будет иконка Python)."""
    try:
        ico = os.path.join(APP_DIR, "app.ico")
        if not os.path.exists(ico):
            return
        hwnd = int(window.wm_frame(), 16)
        WM_SETICON = 0x0080
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x10
        for idx, size in ((1, 32), (0, 16)):      # ICON_BIG, ICON_SMALL
            h = user32.LoadImageW(None, ico, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                user32.SendMessageW(hwnd, WM_SETICON, idx, h)
    except Exception as e:
        log("window icon: %r" % (e,))


# ---------------------------------------------------------------- hotkey
HOTKEY_QUEUE = queue.Queue()
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY = 0x0312


def parse_hotkey(spec):
    spec = (spec or "").lower().replace(" ", "")
    mods = 0
    vk = None
    for part in spec.split("+"):
        if part == "ctrl":
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= 0x0004
        elif part == "win":
            mods |= 0x0008
        elif len(part) == 1:
            vk = ord(part.upper())
        elif part == "space":
            vk = 0x20
    if vk is None:
        return None
    return mods | MOD_NOREPEAT, vk


def hotkey_thread(spec):
    hk = parse_hotkey(spec)
    if not hk:
        return
    mods, vk = hk
    if not user32.RegisterHotKey(None, 1, mods, vk):
        log("hotkey busy: %s" % spec)
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            HOTKEY_QUEUE.put("toggle")
    user32.UnregisterHotKey(None, 1)


# ---------------------------------------------------------------- tray
TRAY_QUEUE = queue.Queue()
WM_TRAY = 0x0400 + 20          # WM_APP + 20
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON)]


class Tray:
    """Иконка в системном трее (как у Rainmeter): правый клик — меню."""

    def __init__(self, ico_path, tip="Панель"):
        self.ok = False
        self.hwnd = None
        self._proc = WNDPROC(self._wndproc)
        try:
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = "DesktopPanelTrayWnd"
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(0, cls_name, "Панель", 0, 0, 0, 0, 0,
                                               None, None, hinst, None)
            if not self.hwnd:
                log("tray: CreateWindow failed %s" % ctypes.get_last_error())
                return
            self.nid = NOTIFYICONDATAW()
            self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self.nid.hWnd = self.hwnd
            self.nid.uID = 1
            self.nid.uFlags = 1 | 2 | 4          # MESSAGE | ICON | TIP
            self.nid.uCallbackMessage = WM_TRAY
            self.nid.szTip = tip[:127]
            hicon = 0
            if ico_path and os.path.exists(ico_path):
                hicon = user32.LoadImageW(None, ico_path, 1, 0, 0, 0x10 | 0x40)  # ICON|LOADFROMFILE|DEFAULTSIZE
            if not hicon:
                hicon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))          # IDI_APPLICATION
            self.nid.hIcon = hicon
            self.ok = bool(shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid)))
            if not self.ok:
                log("tray: Shell_NotifyIcon failed")
        except Exception as e:
            log("tray init: %r" % (e,))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == 0x0205:        # WM_RBUTTONUP
                    TRAY_QUEUE.put("menu")
                elif lparam == 0x0203:      # WM_LBUTTONDBLCLK
                    TRAY_QUEUE.put("toggle")
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def destroy(self):
        try:
            if self.ok:
                shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))  # NIM_DELETE
        except Exception:
            pass


# ---------------------------------------------------------------- settings
class SettingsWindow:
    BG = "#16161A"
    CARD = "#1E1E23"
    FG = "#E7E7EC"
    DIM = "#8E8E99"
    ACCENT = "#E8A33D"

    def __init__(self, panel):
        self.panel = panel
        self.win = tk.Toplevel(panel.root)
        self.win.withdraw()
        self.win.title("Панель — настройки")
        self.win.configure(bg=self.BG)
        self.win.resizable(False, False)
        try:
            self.win.iconbitmap(os.path.join(APP_DIR, "app.ico"))
        except Exception:
            pass

        c = panel.cfg
        font = panel.font
        pad = 18
        w = 500

        self.var_auto = tk.BooleanVar(value=autostart_state())
        self.var_folder = tk.StringVar(value=c["folder"])
        self.var_hotkey = tk.StringVar(value=c.get("hotkey", ""))
        self.scales = {}

        y = pad
        tk.Checkbutton(self.win, text="Запускать вместе с Windows", variable=self.var_auto,
                       bg=self.BG, fg=self.FG, selectcolor=self.CARD,
                       activebackground=self.BG, activeforeground=self.FG,
                       highlightthickness=0, bd=0, anchor="w", font=font).place(x=pad, y=y)
        y += 30
        tk.Label(self.win, text="Видно в диспетчере задач → вкладка «Автозагрузка»",
                 bg=self.BG, fg=self.DIM, font=font).place(x=pad + 24, y=y)
        y += 30

        tk.Label(self.win, text="Папка панели", bg=self.BG, fg=self.FG,
                 font=font).place(x=pad, y=y)
        y += 22
        tk.Entry(self.win, textvariable=self.var_folder, bg=self.CARD, fg=self.FG,
                 insertbackground=self.FG, relief="flat", bd=6,
                 font=font).place(x=pad, y=y, width=w - pad * 2 - 96, height=28)
        tk.Button(self.win, text="Обзор…", command=self.browse, bg="#2E2E37", fg=self.FG,
                  relief="flat", bd=0, activebackground="#3A3A45", activeforeground="#ffffff",
                  font=font).place(x=w - pad - 86, y=y, width=86, height=28)
        y += 42

        rows = [
            ("trigger_width", "Ширина «ручки»", 120, 800, 20, 1, "%d px"),
            ("trigger_alpha", "Прозрачность «ручки»", 5, 60, 1, 100.0, "%d %%"),
            ("panel_alpha", "Прозрачность панели", 20, 100, 1, 100.0, "%d %%"),
            ("columns", "Плиток в ряду", 3, 10, 1, 1, "%d"),
            ("tile_width", "Ширина плитки", 80, 160, 4, 1, "%d px"),
        ]
        for key, label, lo, hi, step, div, fmt in rows:
            val = c[key]
            cur = val * div if key.endswith("alpha") else val
            tk.Label(self.win, text=label, bg=self.BG, fg=self.FG, font=font).place(x=pad, y=y)
            val_lbl = tk.Label(self.win, text=fmt % int(cur), bg=self.BG, fg=self.DIM, font=font)
            val_lbl.place(x=w - pad - 70, y=y, width=70, anchor="ne")
            sc = tk.Scale(self.win, from_=lo, to=hi, resolution=step, orient="horizontal",
                          bg=self.BG, fg=self.FG, troughcolor="#2A2A33", highlightthickness=0,
                          bd=0, sliderrelief="flat", activebackground=self.ACCENT,
                          length=w - pad * 2, font=font, showvalue=0)
            sc.set(cur)
            sc.place(x=pad, y=y + 20, width=w - pad * 2, height=26)
            sc.configure(command=lambda v, l=val_lbl, f=fmt: l.config(text=f % int(float(v))))
            self.scales[key] = (sc, div)
            y += 62

        tk.Label(self.win, text="Горячая клавиша", bg=self.BG, fg=self.FG,
                 font=font).place(x=pad, y=y)
        tk.Entry(self.win, textvariable=self.var_hotkey, bg=self.CARD, fg=self.FG,
                 insertbackground=self.FG, relief="flat", bd=6,
                 font=font).place(x=pad, y=y + 22, width=200, height=28)
        y += 62

        # ---- цвета
        tk.Label(self.win, text="Цвета", bg=self.BG, fg=self.DIM,
                 font=font).place(x=pad, y=y)
        y += 22
        cw, gap_c, ch = 150, 6, 28
        self.swatches = {}
        colors = [("accent", "Акцент"), ("panel_color", "Фон панели"), ("tile_color", "Плитка"),
                  ("text_color", "Текст"), ("border_color", "Рамка"), ("trigger_fill", "«Ручка»")]
        for i, (key, name) in enumerate(colors):
            val = c.get(key, "#000000")
            col, row = i % 3, i // 3
            lbl = tk.Label(self.win, text=name, bg=val, fg=self._contrast(val),
                           font=font, cursor="hand2")
            lbl.place(x=pad + col * (cw + gap_c), y=y + row * (ch + 8), width=cw, height=ch)
            lbl.bind("<Button-1>", lambda e, k=key: self.pick_color(k))
            self.swatches[key] = lbl
        y += 2 * ch + 8 + 22
        tk.Label(self.win, text="Клик по плашке — палитра",
                 bg=self.BG, fg=self.DIM, font=font).place(x=pad, y=y - 14)
        tk.Button(self.win, text="Сбросить цвета", command=self.reset_colors,
                  bg="#2E2E37", fg=self.FG, relief="flat", bd=0,
                  activebackground="#3A3A45", activeforeground="#ffffff",
                  font=font).place(x=w - pad - 170, y=y - 20, width=170, height=26)
        y += 20

        acc = c.get("accent", self.ACCENT)
        tk.Button(self.win, text="Сохранить", command=self.save, bg=acc,
                  fg=self._contrast(acc), relief="flat", bd=0,
                  activebackground=acc, activeforeground=self._contrast(acc),
                  font=font).place(x=w - pad - 200, y=y, width=110, height=32)
        tk.Button(self.win, text="Отмена", command=self.win.destroy, bg="#2E2E37", fg=self.FG,
                  relief="flat", bd=0, activebackground="#3A3A45", activeforeground="#ffffff",
                  font=font).place(x=w - pad - 80, y=y, width=80, height=32)
        y += 48

        self.win.geometry("%dx%d+%d+%d" % (w, y, self.panel.sw // 2 - w // 2,
                                           max(40, self.panel.sh // 2 - y // 2)))
        self.win.update_idletasks()
        apply_window_icon(self.win)
        self.win.deiconify()
        apply_window_icon(self.win)
        self.win.after(120, lambda: apply_window_icon(self.win))
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.armed = False   # клик, которым открыли окно, не должен его закрывать
        self.was_down = False
        self.picking = False  # открыт диалог выбора папки
        self.win.lift()
        self.win.focus_force()

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass

    def reset_colors(self):
        """Вернуть только цвета к заводским (остальные настройки не трогаем)."""
        for key in ("accent", "panel_color", "tile_color", "text_color",
                    "border_color", "trigger_fill"):
            val = DEFAULTS.get(key)
            if not val:
                continue
            self.panel.cfg[key] = val
            lbl = self.swatches.get(key)
            if lbl is not None:
                lbl.config(bg=val, fg=self._contrast(val))

    def _contrast(self, color):
        """Тёмный или светлый текст поверх выбранного цвета."""
        try:
            color = color.lstrip("#")
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            return "#111111" if lum > 140 else "#F0F0F4"
        except Exception:
            return "#F0F0F4"

    def pick_color(self, key):
        from tkinter import colorchooser
        cur = self.panel.cfg.get(key, "#000000")
        try:
            self.picking = True
            rgb, hx = colorchooser.askcolor(color=cur, parent=self.win,
                                            title="Выберите цвет")
        except Exception as e:
            log("colorchooser: %r" % (e,))
            return
        finally:
            self.picking = False
        if hx:
            hx = hx.upper()
            self.panel.cfg[key] = hx
            lbl = self.swatches.get(key)
            if lbl is not None:
                lbl.config(bg=hx, fg=self._contrast(hx))

    def browse(self):
        from tkinter import filedialog
        self.picking = True
        try:
            d = filedialog.askdirectory(initialdir=self.var_folder.get() or os.path.expanduser("~"),
                                        title="Папка для панели")
            if d:
                self.var_folder.set(os.path.normpath(d))
        finally:
            self.picking = False

    def save(self):
        c = self.panel.cfg
        c["folder"] = self.var_folder.get().strip() or c["folder"]
        c["hotkey"] = self.var_hotkey.get().strip()
        for key, (sc, div) in self.scales.items():
            v = float(sc.get())
            c[key] = v / div if div != 1 else int(v)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("settings save: %r" % (e,))

        want = self.var_auto.get()
        set_autostart(want)

        self.win.destroy()
        self.panel.restart()


# ---------------------------------------------------------------- app
class Panel:
    def __init__(self, root):
        self.root = root
        self.cfg = CFG
        self.visible = False
        self.photos = []
        self.content_h = 0

        self.sw, self.sh, self.wl, self.wt, self.wr, self.wb = screen_metrics()

        self.state = load_state()
        self.view = self.state.get("view", "auto")
        self.all_names = []
        self.tile_map = {}
        self.drag_src = None
        self.drag_start = (0, 0)
        self.drag_active = False
        self.drag_hl = None
        self._ticks = 0
        self._was_down = False
        self._last_glue = 0
        # управление с клавиатуры: "tabs" | "tiles" | None
        self.kbd_mode = None
        self.kbd_i = 0
        self.show_tabs = False
        self._tab_info = (0, 0, 0)

        self.font = tkfont.Font(family=self.cfg["font"], size=self.cfg["font_size"])
        self.font_small = tkfont.Font(family=self.cfg["font"], size=max(6, self.cfg["font_size"] - 1))

        self._build_trigger()
        self._build_panel()
        self._bind_global()

        self.menu_open = False
        self.tray = Tray(os.path.join(APP_DIR, "app.ico"), "Панель — быстрый доступ")

        self.root.after(120, self._tick)
        try:
            threading.Thread(target=hotkey_thread, args=(self.cfg.get("hotkey", ""),),
                             daemon=True).start()
        except Exception as e:
            log("hotkey thread: %r" % (e,))

    # ------------------------------------------------------------ windows
    def _build_trigger(self):
        c = self.cfg
        tw, th = c["trigger_width"], c["trigger_height"]
        x = self.sw // 2 - tw // 2
        y = self.wb - th  # вплотную к панели задач, у самого низа
        self.trigger_geo = (x, y, tw, th)
        self.trigger = tk.Toplevel(self.root)
        self.trigger.overrideredirect(True)
        self.trigger.configure(bg=KEY)
        self.trigger.geometry("%dx%d+%d+%d" % (tw, th, x, y))
        try:
            self.trigger.attributes("-transparentcolor", KEY)
        except Exception as e:
            log("transparentcolor: %r" % (e,))
        self.trigger.attributes("-alpha", c["trigger_alpha"])
        # намеренно НЕ topmost: окно живёт на уровне рабочего стола,
        # приложения его перекрывают

        self.tcan = tk.Canvas(self.trigger, width=tw, height=th, bg=KEY,
                              highlightthickness=0, bd=0)
        self.tcan.pack()
        self.pill = self.tcan.create_polygon(
            round_points(1, 1, tw - 1, th - 1, (th - 2) / 2),
            smooth=True, fill=c["trigger_fill"], outline="")

        self.tcan.bind("<Enter>", lambda e: self._hover_trigger(True))
        self.tcan.bind("<Leave>", lambda e: self._hover_trigger(False))
        self.tcan.bind("<Button-1>", lambda e: self.toggle())
        self.tcan.bind("<Button-3>", lambda e: self.menu(e))
        self.root.after(60, self._glue)

    def _glue(self):
        """Держать ручку на уровне рабочего стола: выше обоев, но ниже всех программ."""
        try:
            hwnd = int(self.trigger.wm_frame(), 16)
            ex = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x00000080)  # NOACTIVATE|TOOLWINDOW
            # собираем порядок окон сверху вниз
            order = []
            h = user32.GetTopWindow(None)
            guard = 0
            while h and guard < 3000:
                guard += 1
                order.append(h)
                h = user32.GetWindow(h, 2)  # GW_HWNDNEXT
            if not order:
                return
            desk_idx = None
            for i, wh in enumerate(order):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(wh, cls, 64)
                if cls.value in ("Progman", "WorkerW"):
                    desk_idx = i if desk_idx is None else min(desk_idx, i)
            if desk_idx is None:
                return
            target = order[desk_idx - 1] if desk_idx > 0 else None
            if target == hwnd:
                return  # уже на месте
            if target:
                user32.SetWindowPos(hwnd, target, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            else:
                user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM
        except Exception as e:
            log("glue: %r" % (e,))

    def _hover_trigger(self, on):
        try:
            self.trigger.attributes("-alpha", self.cfg["trigger_alpha_hover"] if on
                                    else self.cfg["trigger_alpha"])
            self.tcan.itemconfig(self.pill, fill=self.cfg["trigger_fill_hover"] if on
                                 else self.cfg["trigger_fill"])
        except Exception:
            pass

    def _build_panel(self):
        c = self.cfg
        self.panel = tk.Toplevel(self.root)
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        self.panel.attributes("-alpha", c["panel_alpha"])
        self.panel.configure(bg=KEY)
        try:
            self.panel.attributes("-transparentcolor", KEY)
        except Exception:
            pass
        self.panel.withdraw()

        self.bgcan = tk.Canvas(self.panel, bg=KEY, highlightthickness=0, bd=0)
        self.bgcan.place(x=0, y=0, relwidth=1, relheight=1)

        self.tabcan = tk.Canvas(self.panel, bg=c["panel_color"], highlightthickness=0, bd=0)
        self.tabcan.place(x=c["padding"], y=c["padding"])

        self.tiles = tk.Canvas(self.panel, bg=c["panel_color"], highlightthickness=0, bd=0)
        self.tiles.place(x=c["padding"], y=c["padding"])
        self.tiles.bind("<MouseWheel>", self._wheel)
        self.tiles.bind("<Button-3>", self._panel_rmb)
        self.tiles.bind("<ButtonPress-1>", self._on_press)
        self.tiles.bind("<B1-Motion>", self._on_drag)
        self.tiles.bind("<ButtonRelease-1>", self._on_release)
        self.panel.bind("<Escape>", lambda e: self.hide())
        self.panel.bind("<Key>", self._on_key)
        self.panel.bind("<MouseWheel>", self._wheel)

    # ------------------------------------------------------------ items
    def scan(self):
        folder = self.cfg["folder"]
        items = []
        try:
            for name in os.listdir(folder):
                if name.startswith(".") or name.lower() in ("desktop.ini", "thumbs.db"):
                    continue
                p = os.path.join(folder, name)
                items.append((os.path.isdir(p), name.lower(), p, name))
        except Exception as e:
            log("scan %s: %r" % (folder, e))
        items.sort(key=lambda t: (not t[0], t[1]))
        return [(p, n) for _d, _s, p, n in items]

    def build_tiles(self):
        c = self.cfg
        self.tiles.delete("all")
        self.tabcan.delete("all")
        self.photos = []
        self.tile_map = {}
        self.drag_hl = None
        self.drag_src = None
        self.drag_active = False

        items = self.scan()
        tw, th, gap = c["tile_width"], c["tile_height"], c["gap"]
        cols = max(1, c["columns"])
        size = c["icon_size"]
        pad = c["padding"]

        alive = set(n for _p, n in items)
        pinned = set(self.state.get("pinned", [])) & alive
        order = [n for n in self.state.get("order", []) if n in alive]
        # подчищаем state, если в нём остались имена удалённых ярлыков
        if pinned != set(self.state.get("pinned", [])) or len(order) != len(self.state.get("order", [])):
            self.state["pinned"] = [n for n in self.state.get("pinned", []) if n in alive]
            self.state["order"] = order
            save_state(self.state)

        oidx = {n: i for i, n in enumerate(order)}
        items.sort(key=lambda t: (oidx.get(t[1], 10 ** 6), t[1].lower()))
        self.all_names = [n for _p, n in items]

        show_tabs = bool(pinned) or len(items) > cols
        if not show_tabs:
            self.view = "all"
        elif self.view not in ("pinned", "all"):
            self.view = "pinned" if pinned else "all"
        if self.view == "pinned" and not pinned:
            self.view = "all"
        self.show_tabs = show_tabs
        if not show_tabs and self.kbd_mode == "tabs":
            # вкладок нет — сразу работаем по плиткам
            self.kbd_mode = "tiles"
            self.kbd_i = 0

        visible = [it for it in items if self.view == "all" or it[1] in pinned]
        entries = list(visible) + [("__add__", "Добавить")]

        n = len(entries)
        rows = (n + cols - 1) // cols
        content_w = cols * tw + (cols - 1) * gap
        self.content_h = rows * th + max(0, rows - 1) * gap

        for i, (path, name) in enumerate(entries):
            r, col = divmod(i, cols)
            x = col * (tw + gap)
            y = r * (th + gap)
            tag = "it%d" % i
            self.tile_map[tag] = (path, name)
            self.tiles.create_polygon(
                round_points(x, y, x + tw, y + th, 12), smooth=True,
                fill=c["tile_color"], outline="", width=0, tags=(tag, tag + "bg", "tile"))

            if path == "__add__":
                cx, cy = x + tw / 2, y + 14
                self.tiles.create_polygon(
                    round_points(cx - 22, cy, cx + 22, cy + size, 10), smooth=True,
                    fill=c["tile_hover"], outline=c["border_color"], width=1, tags=(tag,))
                self.tiles.create_text(cx, cy + size / 2 - 2, text="+",
                                       fill=c["accent"], font=(c["font"], 20), tags=(tag,))
                self.tiles.create_text(x + tw / 2, y + th - 22, text="Добавить",
                                       fill=c["text_dim"], font=self.font, tags=(tag,))
            else:
                img = None
                f = icon_file(path, 48)
                if f:
                    try:
                        pil = Image.open(f).convert("RGBA").resize((size, size), Image.LANCZOS)
                        img = ImageTk.PhotoImage(pil)
                        self.photos.append(img)
                    except Exception:
                        img = None
                iy = y + 14
                if img is not None:
                    self.tiles.create_image(x + tw / 2, iy + size / 2, image=img, tags=(tag,))
                else:
                    self.tiles.create_polygon(
                        round_points(x + tw / 2 - 20, iy + 2, x + tw / 2 + 20, iy + size - 2, 8),
                        smooth=True, fill=c["tile_hover"], outline="", tags=(tag,))
                    self.tiles.create_text(x + tw / 2, iy + size / 2,
                                           text=(short_label(name)[:1] or "?").upper(),
                                           fill=c["accent"], font=(c["font"], 16), tags=(tag,))
                label = self._fit(short_label(name), tw - 12)
                self.tiles.create_text(x + tw / 2, y + th - 22, text=label,
                                       fill=c["text_color"], font=self.font, tags=(tag,))
                if name in pinned:
                    # метка «закреплено»
                    self.tiles.create_oval(x + tw - 17, y + 9, x + tw - 9, y + 17,
                                           fill=c["accent"], outline="", tags=(tag,))

            self.tiles.tag_bind(tag, "<Enter>", lambda e, t=tag: self._hover(t, True))
            self.tiles.tag_bind(tag, "<Leave>", lambda e, t=tag: self._hover(t, False))
            self.tiles.tag_bind(tag, "<Button-3>", lambda e, p=path: self.item_menu(e, p))

        # вкладки
        tab_h = 30 if show_tabs else 0
        self._tab_info = (content_w, len(pinned), len(items))
        if show_tabs:
            self._draw_tabs(*self._tab_info)

        # размеры панели
        w = content_w + pad * 2
        max_rows = max(1, c["max_rows"])
        visible_h = min(self.content_h, max_rows * th + (max_rows - 1) * gap)
        top = pad + (tab_h + 10 if show_tabs else 0)
        h = visible_h + top + pad

        x = self.sw // 2 - w // 2
        y = self.wb - c["trigger_height"] - h - 10
        self.panel_geo = (x, y, w, h)
        self.panel.geometry("%dx%d+%d+%d" % (w, h, x, y))

        self.bgcan.config(width=w, height=h)
        self.bgcan.delete("all")
        self.bgcan.create_polygon(round_points(1, 1, w - 1, h - 1, c["radius"]),
                                  smooth=True, fill=c["panel_color"],
                                  outline=c["border_color"], width=1)

        if show_tabs:
            self.tabcan.config(width=content_w, height=tab_h)
            self.tabcan.place(x=pad, y=pad, width=content_w, height=tab_h)
        else:
            self.tabcan.place_forget()

        self.tiles.config(width=content_w, height=visible_h)
        self.tiles.configure(scrollregion=(0, 0, content_w, max(self.content_h, visible_h)))
        self.tiles.place(x=pad, y=top, width=content_w, height=visible_h)
        self.tiles.yview_moveto(0)
        self.view_h = visible_h
        if self.kbd_mode == "tiles":
            self._kbd_draw()

    def _draw_tabs(self, width, n_pinned, n_all):
        c = self.cfg
        tabs = [("pinned", "Закреплённые", n_pinned), ("all", "Все", n_all)]
        x = 2
        for key, label, cnt in tabs:
            active = (self.view == key)
            color = c["text_color"] if active else c["text_dim"]
            tag = "tab_" + key
            item = self.tabcan.create_text(x, 13, text=label, anchor="w",
                                           fill=color, font=self.font, tags=(tag,))
            bb = self.tabcan.bbox(item)
            end = bb[2]
            if cnt:
                num = str(cnt)
                nw = max(20, self.font_small.measure(num) + 14)
                bx = end + 7
                self.tabcan.create_polygon(
                    round_points(bx, 4, bx + nw, 22, 9), smooth=True,
                    fill="#31313B" if active else "#232329", outline="", tags=(tag,))
                self.tabcan.create_text(bx + nw / 2, 13, text=num, fill=color,
                                        font=self.font_small, tags=(tag,))
                end = bx + nw
            if active:
                self.tabcan.create_line(bb[0], 26, end, 26, fill=c["accent"], width=2,
                                        tags=(tag,))
            self.tabcan.tag_bind(tag, "<Button-1>", lambda e, k=key: self.set_view(k))
            self.tabcan.tag_bind(tag, "<Enter>",
                                 lambda e: self.tabcan.config(cursor="hand2"))
            self.tabcan.tag_bind(tag, "<Leave>",
                                 lambda e: self.tabcan.config(cursor=""))
            x = end + 22

    def set_view(self, key):
        self.view = key
        self.build_tiles()

    # ------------------------------------------------------------ клавиатура
    KEYMAP = {
        "a": "left", "ф": "left", "cyrillic_ef": "left", "left": "left",
        "d": "right", "в": "right", "cyrillic_ve": "right", "right": "right",
        "w": "up", "ц": "up", "cyrillic_tse": "up", "up": "up",
        "s": "down", "ы": "down", "cyrillic_yeru": "down", "down": "down",
        "e": "enter", "у": "enter", "cyrillic_u": "enter",
        "return": "enter", "kp_enter": "enter",
        "q": "back", "й": "back", "cyrillic_shorti": "back",
    }

    def _on_key(self, e):
        """A/D — вкладки, E — вниз к плиткам, WASD — по плиткам, E — запуск, Q — назад."""
        if not self.visible:
            return None
        ch = (getattr(e, "char", "") or "").lower()
        ks = (getattr(e, "keysym", "") or "").lower()
        action = self.KEYMAP.get(ch) or self.KEYMAP.get(ks)
        if not action:
            return None

        if self.kbd_mode == "tabs":
            if action in ("left", "right"):
                if self.show_tabs:
                    self._kbd_tab(-1 if action == "left" else 1)
            elif action in ("down", "enter"):
                self.kbd_mode = "tiles"
                self.kbd_i = 0
                self._kbd_draw()
            elif action == "back":
                self.hide()
            return "break"

        if self.kbd_mode != "tiles":
            return None

        cols = max(1, self.cfg["columns"])
        n = len(self.tile_map)
        if action == "enter":
            ent = self.tile_map.get("it%d" % self.kbd_i)
            if ent:
                self._activate(ent[0])
            return "break"
        if action == "back":
            if self.show_tabs:
                self.kbd_mode = "tabs"
                self._kbd_clear()
            else:
                self.hide()
            return "break"
        if action == "left":
            self.kbd_i -= 1
        elif action == "right":
            self.kbd_i += 1
        elif action == "up":
            self.kbd_i -= cols
        elif action == "down":
            self.kbd_i += cols
        if n:
            self.kbd_i = max(0, min(self.kbd_i, n - 1))
        self._kbd_draw()
        return "break"

    def _kbd_tab(self, step):
        keys = ["pinned", "all"]
        i = keys.index(self.view) if self.view in keys else 1
        self.set_view(keys[(i + step) % len(keys)])

    def _kbd_clear(self):
        try:
            self.tiles.delete("kbfocus")
        except Exception:
            pass

    def _kbd_draw(self):
        """Рамка вокруг плитки, выбранной с клавиатуры."""
        self._kbd_clear()
        if self.kbd_mode != "tiles" or not self.tile_map:
            return
        c = self.cfg
        tw, th, gap = c["tile_width"], c["tile_height"], c["gap"]
        cols = max(1, c["columns"])
        self.kbd_i = max(0, min(self.kbd_i, len(self.tile_map) - 1))
        r, col = divmod(self.kbd_i, cols)
        x = col * (tw + gap)
        y = r * (th + gap)
        self.tiles.create_polygon(
            round_points(x + 1, y + 1, x + tw - 1, y + th - 1, 12), smooth=True,
            fill="", outline=c["accent"], width=2, tags=("kbfocus",))
        self._ensure_visible(self.kbd_i, y)

    def _ensure_visible(self, i, y):
        c = self.cfg
        tw, th, gap = c["tile_width"], c["tile_height"], c["gap"]
        total = max(1, self.content_h)
        view_h = max(1, getattr(self, "view_h", 0) or self.tiles.winfo_height())
        top = self.tiles.canvasy(0)
        if y < top:
            self.tiles.yview_moveto(y / total)
        elif y + th > top + view_h:
            self.tiles.yview_moveto((y + th - view_h) / total)

    # ------------------------------------------------------------ drag & drop
    def _tile_index_at(self, event):
        c = self.cfg
        tw, th, gap = c["tile_width"], c["tile_height"], c["gap"]
        cols = max(1, c["columns"])
        x = self.tiles.canvasx(event.x)
        y = self.tiles.canvasy(event.y)
        if x < 0 or y < 0:
            return None
        col = int(x // (tw + gap))
        row = int(y // (th + gap))
        if col >= cols:
            return None
        if x - col * (tw + gap) > tw or y - row * (th + gap) > th:
            return None
        idx = row * cols + col
        return idx if 0 <= idx < len(self.tile_map) else None

    def _on_press(self, event):
        self.drag_src = self._tile_index_at(event)
        self.drag_start = (event.x_root, event.y_root)
        self.drag_active = False

    def _on_drag(self, event):
        if self.drag_src is None:
            return
        dx = abs(event.x_root - self.drag_start[0])
        dy = abs(event.y_root - self.drag_start[1])
        if not self.drag_active and max(dx, dy) < 8:
            return
        self.drag_active = True
        self.tiles.config(cursor="fleur")
        idx = self._tile_index_at(event)
        tag = "it%d" % idx if idx is not None else None
        if tag != self.drag_hl:
            if self.drag_hl and self.drag_hl in self.tile_map:
                self.tiles.itemconfig(self.drag_hl + "bg", outline="", width=0)
            self.drag_hl = tag
            if tag and self.tile_map.get(tag, ("", ""))[0] != "__add__":
                self.tiles.itemconfig(tag + "bg", outline=self.cfg["accent"], width=2)

    def _on_release(self, event):
        self.tiles.config(cursor="")
        if self.drag_hl and self.drag_hl in self.tile_map:
            try:
                self.tiles.itemconfig(self.drag_hl + "bg", outline="", width=0)
            except Exception:
                pass
        if self.drag_src is None:
            return
        if self.drag_active:
            dst = self._tile_index_at(event)
            if dst is not None and dst != self.drag_src:
                self._reorder(self.drag_src, dst)
        else:
            ent = self.tile_map.get("it%d" % self.drag_src)
            if ent:
                self._activate(ent[0])
        self.drag_src = None
        self.drag_active = False
        self.drag_hl = None

    def _reorder(self, src_idx, dst_idx):
        src = self.tile_map.get("it%d" % src_idx)
        dst = self.tile_map.get("it%d" % dst_idx)
        if not src or not dst or "__add__" in (src[0], dst[0]):
            return
        names = list(self.all_names)
        if src[1] not in names or dst[1] not in names:
            return
        names.remove(src[1])
        names.insert(names.index(dst[1]), src[1])
        self.state["order"] = names
        save_state(self.state)
        self.build_tiles()

    def _fit(self, text, max_w):
        if self.font.measure(text) <= max_w:
            return text
        while text and self.font.measure(text + "…") > max_w:
            text = text[:-1]
        return text + "…" if text else "…"

    def _hover(self, tag, on):
        c = self.cfg
        try:
            self.tiles.itemconfig(tag + "bg", fill=c["tile_hover"] if on else c["tile_color"])
        except Exception:
            pass

    def _wheel(self, e):
        if self.content_h <= self.tiles.winfo_height():
            return
        self.tiles.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def _activate(self, path):
        if path == "__add__":
            self.open_folder()
            self.hide()
            return
        try:
            os.startfile(path)
        except Exception:
            try:
                subprocess.Popen(["explorer", "/select,", path])
            except Exception as e:
                log("launch %s: %r" % (path, e))
        self.hide()

    # ------------------------------------------------------------ show/hide
    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()
        self.root.after(30, self._glue)

    def show(self):
        if self.visible:
            return
        # при открытии всегда показываем «Закреплённые», если там что-то есть
        self.view = "pinned" if self.state.get("pinned") else "all"
        self.kbd_mode = "tabs"       # открылись на вкладках: A/D переключают
        self.kbd_i = 0
        self.build_tiles()
        self.visible = True
        x, y, w, h = self.panel_geo
        try:
            self.panel.deiconify()
            self.panel.lift()
            self.panel.attributes("-topmost", True)
            self.panel.focus_force()
        except Exception as e:
            log("show: %r" % (e,))
        self._animate(0.0, y + 14, y, 1)

    def _animate(self, alpha, y_from, y_to, step):
        steps = 6
        if step > steps:
            self.panel.attributes("-alpha", self.cfg["panel_alpha"])
            return
        a = alpha + (self.cfg["panel_alpha"] - alpha) * step / steps
        y = int(y_from + (y_to - y_from) * step / steps)
        x = self.panel_geo[0]
        self.panel.geometry("+%d+%d" % (x, y))
        try:
            self.panel.attributes("-alpha", a)
        except Exception:
            pass
        self.panel.after(12, lambda: self._animate(a, y_from, y_to, step + 1))

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.kbd_mode = None
        self._kbd_clear()
        self.panel.withdraw()

    # ------------------------------------------------------------ menu
    def menu(self, event):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Настройки…", command=self.open_settings)
        m.add_command(label="Открыть папку панели", command=self.open_folder)
        m.add_separator()
        auto = autostart_state()
        m.add_command(label=("Автозапуск: включён  ✓" if auto else "Автозапуск: выключен"),
                      command=self.toggle_autostart)
        m.add_separator()
        m.add_command(label="config.json", command=self.open_config)
        m.add_command(label="Перезапустить", command=self.restart)
        m.add_command(label="Выход", command=self.quit)
        self._popup(m, event.x_root, event.y_root)

    def tray_menu(self):
        x, y = cursor_pos()
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Открыть панель", command=self.toggle)
        m.add_command(label="Настройки…", command=self.open_settings)
        m.add_command(label="Открыть папку панели", command=self.open_folder)
        m.add_separator()
        auto = autostart_state()
        m.add_command(label=("Автозапуск: включён  ✓" if auto else "Автозапуск: выключен"),
                      command=self.toggle_autostart)
        m.add_separator()
        m.add_command(label="Выход", command=self.quit)
        self._popup(m, x, y)

    def open_settings(self):
        try:
            cur = getattr(self, "_settings", None)
            if cur is not None:
                try:
                    if cur.win.winfo_exists():
                        cur.win.lift()
                        cur.win.focus_force()
                        return
                except Exception:
                    pass
            self.hide()
            self._settings = SettingsWindow(self)
        except Exception as e:
            log("settings window: %r" % (e,))

    def _watch_settings(self):
        """Закрыть окно настроек кликом мимо (только если нажатие началось вне окна)."""
        sw = getattr(self, "_settings", None)
        if sw is None:
            return
        try:
            if not sw.win.winfo_exists():
                self._settings = None
                return
            down = left_button_down()
            if not sw.armed:
                if not down:
                    sw.armed = True
                sw.was_down = down
                return
            if self.menu_open or sw.picking or sw.win.grab_current() is not None:
                sw.was_down = down
                return
            if down and not sw.was_down:
                # новое нажатие: закрываем только если оно началось вне окна
                x, y = cursor_pos()
                wx, wy = sw.win.winfo_rootx(), sw.win.winfo_rooty()
                ww, wh = sw.win.winfo_width(), sw.win.winfo_height()
                if not (wx <= x <= wx + ww and wy <= y <= wy + wh):
                    sw.close()
                    self._settings = None
            sw.was_down = down
        except Exception as e:
            log("watch settings: %r" % (e,))

    def item_menu(self, event, path):
        """Меню конкретного ярлыка: открыть / показать / закрепить / убрать."""
        if path == "__add__":
            self.open_folder()
            self.hide()
            return
        name = os.path.basename(path)
        pinned = name in set(self.state.get("pinned", []))
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Открыть", command=lambda: self._activate(path))
        m.add_command(label="Показать в проводнике", command=lambda: self.reveal(path))
        m.add_separator()
        m.add_command(label=("Открепить" if pinned else "Закрепить"),
                      command=lambda: self.toggle_pin(name))
        m.add_separator()
        m.add_command(label="Убрать с панели (в корзину)", command=lambda: self.remove_item(path))
        self._popup(m, event.x_root, event.y_root)

    def toggle_pin(self, name):
        pinned = list(self.state.get("pinned", []))
        if name in pinned:
            pinned.remove(name)
        else:
            pinned.append(name)
        self.state["pinned"] = pinned
        save_state(self.state)
        self.build_tiles()

    def toggle_autostart(self):
        set_autostart(not autostart_state())

    def _popup(self, m, x, y):
        self.menu_open = True
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()
            self.menu_open = False

    def _panel_rmb(self, event):
        """Правый клик по пустому месту панели (у плиток своё меню)."""
        cur = self.tiles.find_withtag("current")
        if cur:
            tags = self.tiles.gettags(cur[0])
            if any(t.startswith("it") for t in tags):
                return
        self.menu(event)

    def reveal(self, path):
        try:
            subprocess.Popen(["explorer", "/select,", path])
        except Exception as e:
            log("reveal %s: %r" % (path, e))

    def remove_item(self, path):
        """Убрать элемент из панели — в корзину (восстановить можно)."""
        try:
            op = SHFILEOPSTRUCTW()
            op.hwnd = None
            op.wFunc = 3                 # FO_DELETE
            op.pFrom = path + "\0"
            op.fFlags = 0x40 | 0x10 | 0x4  # ALLOWUNDO | NOCONFIRMATION | SILENT
            res = shell32.SHFileOperationW(ctypes.byref(op))
            if res != 0:
                log("remove %s: SHFileOperation=%s" % (path, res))
        except Exception as e:
            log("remove %s: %r" % (path, e))
        self.build_tiles()

    def open_folder(self):
        folder = self.cfg["folder"]
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)
        except Exception as e:
            log("open folder: %r" % (e,))

    def open_config(self):
        if not os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        try:
            os.startfile(CONFIG_PATH)
        except Exception:
            subprocess.Popen(["notepad.exe", CONFIG_PATH])

    def restart(self):
        log("restart requested")
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        global MUTEX_HANDLE
        try:
            if MUTEX_HANDLE:
                kernel32.CloseHandle(MUTEX_HANDLE)
                MUTEX_HANDLE = None
        except Exception:
            pass
        subprocess.Popen([exe, os.path.abspath(__file__)])
        self.quit()

    def quit(self):
        try:
            self.tray.destroy()
        except Exception:
            pass
        try:
            self.panel.destroy()
            self.trigger.destroy()
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------------------------------------ loop
    def _bind_global(self):
        self.root.bind_all("<Escape>", lambda e: self.hide())

    def _tick(self):
        try:
            while True:
                msg = HOTKEY_QUEUE.get_nowait()
                if msg == "toggle":
                    self.toggle()
        except queue.Empty:
            pass

        try:
            while True:
                msg = TRAY_QUEUE.get_nowait()
                if msg == "toggle":
                    self.toggle()
                elif msg == "menu":
                    self.tray_menu()
        except queue.Empty:
            pass

        self._ticks += 1
        now = time.time()
        if now - getattr(self, "_last_glue", 0) >= 2.0:
            self._last_glue = now
            self._glue()

        if self.visible or getattr(self, "_settings", None) is not None:
            self._watch_settings()

            down = left_button_down()
            new_press = down and not self._was_down
            self._was_down = down
            if self.visible and not self.menu_open and new_press:
                x, y = cursor_pos()
                px, py, pw, ph = self.panel_geo
                tx, ty, tw, th = self.trigger_geo
                in_panel = px <= x <= px + pw and py <= y <= py + ph
                in_trigger = tx <= x <= tx + tw and ty <= y <= ty + th
                if not in_panel and not in_trigger:
                    self.hide()
            self.root.after(16, self._tick)
        else:
            # всё закрыто — просто ждём горячую клавишу/трей, не крутим цикл вхолостую
            self._was_down = left_button_down()
            self.root.after(120, self._tick)


def main():
    setup_dpi()

    # чтобы панель задач брала иконку из ярлыка «Панель», а не иконку Python
    try:
        shell32.SetCurrentProcessExplicitAppUserModelID("Nous.DesktopPanel")
    except Exception as e:
        log("aumid: %r" % (e,))

    # единственный экземпляр
    global MUTEX_HANDLE
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    MUTEX_HANDLE = k32.CreateMutexW(None, False, "Global\\DesktopPanelSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        log("already running")
        return

    folder = CFG["folder"]
    os.makedirs(folder, exist_ok=True)
    os.makedirs(ICON_CACHE_DIR, exist_ok=True)
    cleanup_icon_cache()

    # миграция: старый ярлык в Автозагрузке -> ключ Run
    try:
        if os.path.exists(STARTUP_LNK) and not autostart_state():
            set_autostart(True)
    except Exception as e:
        log("autostart migrate: %r" % (e,))

    root = tk.Tk()
    root.withdraw()

    def _tk_error(exc, val, tb):
        import traceback as _tb
        log("Tk error: " + "".join(_tb.format_exception(exc, val, tb)))

    root.report_callback_exception = _tk_error
    try:
        root.tk.call("tk", "scaling", user32.GetDpiForSystem() / 72.0)
    except Exception:
        pass
    Panel(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("FATAL " + traceback.format_exc())
        raise
