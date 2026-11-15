# -*- coding: utf-8 -*-
"""野狐窗口 / 控件 / 点击 / 键盘 —— 纯 ctypes，不依赖 pywin32。

为什么要这个模块
----------------
大模型给的是「截图里的 (x, y)」，落地得变成「屏幕上的物理坐标」，
再决定这一下点击是**普通鼠标**（SetCursorPos + mouse_event）还是
**直达窗口**（PostMessage）/ **按控件**（BM_CLICK）。这些都在这里。

要点（都是实战踩出来的）
------------------------
- **按 PID 找窗口**，不靠标题（``fox_board.py`` 2026-10-22 的教训：
  标题 "> [高级房1]" 一条规则都不命中）。
- **野狐有多个顶层窗口**：主窗口之外，「围棋精灵陪练」是独立
  ``#32770`` 顶层窗（NOTES.md 第 8 节），所以别只看最大的那个。
- **最小化时 GetWindowRect 是假的**（158x26）。这里如实标记
  ``Rect.minimized``，还原窗口是调用方的显式决定（``allow_restore``）。
- **鼠标点击打的是屏幕绝对坐标，不认窗口层级**：野狐被盖住时会点到别人
  身上，所以 ``activate()`` 放在这里，``actions`` 在非前台时改走 PostMessage。
- **中文输入用 SendInput + KEYEVENTF_UNICODE**，不依赖输入法。

用法::

    from agent import win32

    fox = win32.find_fox_window()          # FoxWindow(hwnd, title, pid, rect)
    win32.activate(fox.hwnd)               # True = 确实在前台
    win32.click_at(812, 340)               # 普通鼠标点击（物理坐标）
    win32.post_click(fox.hwnd, 812, 340)   # 绕开遮挡，直送控件
    win32.send_unicode_text("你好，请多指教")
    win32.send_key("Enter")

坐标约定：本模块所有 ``x, y`` 都是**屏幕物理像素**（除非名字里带
``client``）；``Shot``/``Annotation`` 到屏幕的换算在 capture/annotate 里。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

__all__ = [
    "IS_WINDOWS",
    "FOX_PROCESS",
    "Win32Error",
    "Win32Unavailable",
    "FoxWindowNotFound",
    "Rect",
    "FoxWindow",
    "Control",
    "VK",
    "HELPER_WINDOW_CLASSES",
    "is_helper_window",
    "pids_of",
    "top_level_windows",
    "list_fox_windows",
    "find_fox_windows",
    "find_fox_window",
    "wait_for_fox_window",
    "fox_state",
    "is_process_running",
    "window_text",
    "window_class",
    "window_pid",
    "get_window_rect",
    "get_client_rect",
    "client_to_screen",
    "screen_to_client",
    "is_window",
    "is_visible",
    "is_minimized",
    "foreground_hwnd",
    "is_foreground",
    "is_window_on_screen",
    "window_dpi",
    "window_from_point",
    "window_at_point",
    "deepest_child_at",
    "describe_point",
    "direct_children",
    "walk_controls",
    "enum_children",
    "find_control",
    "find_controls",
    "control_text",
    "set_window_text",
    "enable_dpi_awareness",
    "activate",
    "bring_to_top",
    "show_window",
    "click_at",
    "double_click_at",
    "right_click_at",
    "scroll_at",
    "post_click",
    "bm_click",
    "send_unicode_text",
    "send_key",
    "send_keys",
    "normalize_key",
    "make_lparam",
    "split_lparam",
]

IS_WINDOWS = os.name == "nt"
FOX_PROCESS = "foxwq.exe"

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
BM_CLICK = 0x00F5

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004

GA_PARENT = 1
GA_ROOT = 2
GA_ROOTOWNER = 3

GW_CHILD = 5
GW_HWNDNEXT = 2

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# SendInput
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# 键盘名 → 虚拟键码（SendInput 用 VK）
VK: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "spacebar": 0x20,
    "backspace": 0x08, "back": 0x08, "bs": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "arrowup": 0x26, "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "pgup": 0x21, "pgdn": 0x22,
    "prior": 0x21, "next": 0x22,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "menu": 0x12,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "pause": 0x13,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69,
    "add": 0x6B, "subtract": 0x6D, "multiply": 0x6A, "divide": 0x6F,
}
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i        # F1=0x70 ... F12=0x7B

_MODIFIER_NAMES = {
    "ctrl": "ctrl", "control": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "shift": "shift", "alt": "alt", "menu": "alt",
    "win": "win", "lwin": "win", "rwin": "win",
}
_MODIFIER_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}

_KEY_ALIAS = {
    "return": "enter", "escape": "esc", "esc": "esc",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "pgup": "pageup", "pgdn": "pagedown", "prior": "pageup", "next": "pagedown",
    "bs": "backspace", "del": "delete", "ins": "insert",
    "spacebar": "space", "space": "space",
}


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------
class Win32Error(RuntimeError):
    """Win32 调用失败 / 环境不支持。"""


class Win32Unavailable(Win32Error):
    """不在 Windows 上（或 DLL 加载失败）。"""


class FoxWindowNotFound(Win32Error):
    """找不到野狐窗口。"""


# --------------------------------------------------------------------------
# DLL / 原型（只在 Windows 上初始化）
# --------------------------------------------------------------------------
_user32: Any = None
_kernel32: Any = None
_WNDENUMPROC: Any = None


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _setup_prototypes() -> None:
    """一次性给所有函数定 argtypes/restype。

    ⚠️ 64 位下 HWND 是 8 字节，不设 restype 会被截成 c_int
    （``WindowFromPoint`` 返回的句柄高位丢了 → 找不到窗口）。
    """
    global _WNDENUMPROC
    u = _user32
    k = _kernel32

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    for name in ("EnumWindows", "EnumChildWindows"):
        fn = getattr(u, name)
        fn.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        fn.restype = wintypes.BOOL

    u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetWindow.restype = wintypes.HWND
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetAncestor.restype = wintypes.HWND
    u.GetParent.argtypes = [wintypes.HWND]
    u.GetParent.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetClientRect.restype = wintypes.BOOL
    u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.ClientToScreen.restype = wintypes.BOOL
    u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.ScreenToClient.restype = wintypes.BOOL
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindow.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.IsWindowEnabled.argtypes = [wintypes.HWND]
    u.IsWindowEnabled.restype = wintypes.BOOL
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.BringWindowToTop.restype = wintypes.BOOL
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT]
    u.SetWindowPos.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.SetCursorPos.restype = wintypes.BOOL
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.GetCursorPos.restype = wintypes.BOOL
    u.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, ctypes.c_size_t]
    u.mouse_event.restype = None
    u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    u.SendInput.restype = wintypes.UINT
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.WindowFromPoint.restype = wintypes.HWND
    u.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
    u.ChildWindowFromPointEx.restype = wintypes.HWND
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                               wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                               wintypes.LPARAM]
    u.SendMessageW.restype = ctypes.c_ssize_t
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.restype = ctypes.c_int

    # Win10 1607+；老系统上没这个导出，用 try 设
    for name, argtypes, restype in (
        ("GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
        ("SetProcessDpiAwarenessContext", [ctypes.c_void_p], wintypes.BOOL),
        ("SetProcessDPIAware", [], wintypes.BOOL),
        ("GetDoubleClickTime", [], wintypes.UINT),
    ):
        try:
            fn = getattr(u, name)
            fn.argtypes = argtypes
            fn.restype = restype
        except AttributeError:
            pass

    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.GetCurrentThreadId.argtypes = []
    k.GetCurrentThreadId.restype = wintypes.DWORD


if IS_WINDOWS:
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _setup_prototypes()
    except OSError as _exc:      # pragma: no cover - 理论上不会
        _user32 = _kernel32 = None
        _SETUP_ERROR = str(_exc)
    else:
        _SETUP_ERROR = ""


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise Win32Unavailable("win32 模块只能在 Windows 上使用（当前不是 Windows）")
    if _user32 is None or _kernel32 is None:
        raise Win32Unavailable(f"加载 user32/kernel32 失败：{_SETUP_ERROR}")


# --------------------------------------------------------------------------
# 纯函数：坐标打包 / 按键名
# --------------------------------------------------------------------------
def make_lparam(x: int, y: int) -> int:
    """把客户区坐标打成 WM_* 用的 LPARAM（低 16 位 x，高 16 位 y）。"""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def split_lparam(lparam: int) -> tuple[int, int]:
    """``make_lparam`` 的逆运算（有符号 16 位）。"""
    value = int(lparam)
    x = value & 0xFFFF
    y = (value >> 16) & 0xFFFF
    if x >= 0x8000:
        x -= 0x10000
    if y >= 0x8000:
        y -= 0x10000
    return x, y


def normalize_key(name: str) -> tuple[tuple[str, ...], int, str]:
    """解析按键名 → ``(修饰键们, 虚拟键码, 规范名)``。

    支持 ``"Enter"`` / ``"esc"`` / ``"up"`` / ``"ctrl+a"`` / ``"ctrl+shift+Tab"``。
    非法名字抛 ``ValueError``（纯函数，不碰硬件，``actions`` 用它做校验）。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("按键名不能为空")
    raw = name.strip()
    parts = [p.strip() for p in raw.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError(f"按键名无法解析：{name!r}")
    modifiers: list[str] = []
    base: str | None = None
    for part in parts:
        low = part.lower()
        if low in _MODIFIER_NAMES:
            mod = _MODIFIER_NAMES[low]
            if mod not in modifiers:
                modifiers.append(mod)
            continue
        if base is not None:
            raise ValueError(f"按键名里只能有一个主键：{name!r}")
        base = low
    if base is None:
        raise ValueError(f"按键名缺少主键：{name!r}")

    if base in _MODIFIER_NAMES or base in _MODIFIER_VK:
        raise ValueError(f"按键名只有修饰键、缺少主键：{name!r}（例如 ctrl+a）")
    canon = _KEY_ALIAS.get(base, base)
    if canon in VK and canon not in _MODIFIER_VK:
        vk = VK[canon]
    elif len(base) == 1 and base.isalnum():
        vk = ord(base.upper())
        canon = base.lower()
    else:
        raise ValueError(f"不认识的按键：{name!r}（可用 Enter/Esc/Tab/方向键/字母数字/ctrl+a 等）")
    display = "+".join(modifiers + [canon]) if modifiers else canon
    return tuple(modifiers), vk, display


# --------------------------------------------------------------------------
# dataclass
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Rect:
    """屏幕矩形（``width``/``height``，不是 right/bottom）。"""

    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0

    @classmethod
    def from_ltrb(cls, left: int, top: int, right: int, bottom: int) -> "Rect":
        return cls(int(left), int(top), int(right) - int(left), int(bottom) - int(top))

    @classmethod
    def from_size(cls, left: int, top: int, width: int, height: int) -> "Rect":
        return cls(int(left), int(top), int(width), int(height))

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: float, y: float, margin: int = 0) -> bool:
        return (self.left - margin <= x <= self.right + margin
                and self.top - margin <= y <= self.bottom + margin)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    def to_dict(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    def __str__(self) -> str:        # 方便打日志
        return f"({self.left},{self.top},{self.width}x{self.height})"


@dataclass(frozen=True)
class FoxWindow:
    """野狐的一个顶层窗口。"""

    hwnd: int
    title: str = ""
    pid: int = 0
    cls: str = ""
    rect: Rect = field(default_factory=Rect)
    visible: bool = True
    minimized: bool = False
    enabled: bool = True

    @property
    def area(self) -> int:
        return self.rect.area

    @property
    def center(self) -> tuple[int, int]:
        return self.rect.center

    @property
    def is_dialog(self) -> bool:
        """类名是 ``#32770``（MFC 对话框）。

        ⚠️ 野狐主窗口**也是**这个类（2026-11-05 实测），别拿它认对话框；
        要区分只能靠面积 + 标题 + ``HELPER_WINDOW_CLASSES``。
        """
        return self.cls == "#32770"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "pid": self.pid,
            "class": self.cls,
            "rect": self.rect.to_dict(),
            "visible": self.visible,
            "minimized": self.minimized,
            "dialog": self.is_dialog,
        }

    def __str__(self) -> str:
        flag = " [最小化]" if self.minimized else ""
        return (f"FoxWindow(hwnd={self.hwnd}, pid={self.pid}, "
                f"title={self.title!r}, rect={self.rect}, class={self.cls!r}){flag}")


@dataclass(frozen=True)
class Control:
    """子控件（``depth`` 从 1 开始，1 = 直接子窗口）。"""

    hwnd: int
    cls: str = ""
    text: str = ""
    rect: Rect = field(default_factory=Rect)
    visible: bool = True
    enabled: bool = True
    depth: int = 1
    parent: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return self.rect.center

    @property
    def area(self) -> int:
        return self.rect.area

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "class": self.cls,
            "text": self.text,
            "rect": self.rect.to_dict(),
            "visible": self.visible,
            "enabled": self.enabled,
            "depth": self.depth,
        }

    def __str__(self) -> str:
        return (f"Control(hwnd={self.hwnd}, class={self.cls!r}, "
                f"text={self.text!r}, rect={self.rect}, depth={self.depth})")


# --------------------------------------------------------------------------
# 窗口枚举
# --------------------------------------------------------------------------
def pids_of(exe_name: str = FOX_PROCESS) -> list[int]:
    """按进程名（不区分大小写）列出所有 PID（toolhelp 快照，不弹窗）。"""
    _require_windows()
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out: list[int] = []
    try:
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe_name.lower():
                out.append(int(entry.th32ProcessID))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def is_process_running(exe_name: str = FOX_PROCESS) -> bool:
    try:
        return bool(pids_of(exe_name))
    except Win32Error:
        return False


def top_level_windows() -> list[int]:
    """所有顶层窗口句柄（含不可见、含其它进程）。"""
    _require_windows()
    hits: list[int] = []

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):        # noqa: ANN001 - ctypes 回调
        hits.append(int(hwnd))
        return True

    _user32.EnumWindows(_cb, 0)
    return hits


def window_pid(hwnd: int) -> int:
    _require_windows()
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _rect_of(hwnd: int) -> Rect:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return Rect()
    return Rect.from_ltrb(rect.left, rect.top, rect.right, rect.bottom)


def get_window_rect(hwnd: int) -> Rect:
    """窗口屏幕矩形。⚠️ 最小化时是假尺寸（通常 158x26），看 ``is_minimized``。"""
    _require_windows()
    return _rect_of(hwnd)


def get_client_rect(hwnd: int) -> Rect:
    """客户区尺寸（left/top 恒为 0，宽高是真的）。"""
    _require_windows()
    rect = wintypes.RECT()
    if not _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return Rect()
    return Rect.from_ltrb(0, 0, rect.right, rect.bottom)


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    _require_windows()
    point = wintypes.POINT(int(x), int(y))
    if not _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point)):
        raise Win32Error(f"ClientToScreen 失败 hwnd={hwnd} ({x},{y})")
    return (int(point.x), int(point.y))


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    _require_windows()
    point = wintypes.POINT(int(x), int(y))
    if not _user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(point)):
        raise Win32Error(f"ScreenToClient 失败 hwnd={hwnd} ({x},{y})")
    return (int(point.x), int(point.y))


def window_text(hwnd: int, maxlen: int = 512) -> str:
    """窗口标题（跨进程安全，走 GetWindowTextW）。"""
    _require_windows()
    length = _user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(min(int(maxlen), length + 1))
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, len(buf))
    return buf.value


def window_class(hwnd: int, maxlen: int = 256) -> str:
    _require_windows()
    buf = ctypes.create_unicode_buffer(int(maxlen))
    _user32.GetClassNameW(wintypes.HWND(hwnd), buf, len(buf))
    return buf.value


def control_text(hwnd: int, maxlen: int = 4096) -> str:
    """读控件文本（WM_GETTEXT，能读 Edit/RICHEDIT；跨进程 SendMessage 有卡死风险，
    只在你确实需要读输入框时调用）。"""
    _require_windows()
    buf = ctypes.create_unicode_buffer(int(maxlen))
    _user32.SendMessageW(wintypes.HWND(hwnd), WM_GETTEXT, int(maxlen),
                         ctypes.addressof(buf))
    return buf.value


def set_window_text(hwnd: int, text: str, *, send: bool = True) -> bool:
    """``WM_SETTEXT`` 直接给控件塞文本（聊天框可先塞再按 Enter）。

    跨进程 SendMessage 对卡死的窗口会阻塞；``send=False`` 改走 PostMessage。
    """
    _require_windows()
    buf = ctypes.create_unicode_buffer(str(text))
    func = _user32.SendMessageW if send else _user32.PostMessageW
    result = func(wintypes.HWND(hwnd), WM_SETTEXT, 0, ctypes.addressof(buf))
    return bool(result) if send else bool(result)


def is_window(hwnd: int) -> bool:
    _require_windows()
    return bool(hwnd) and bool(_user32.IsWindow(wintypes.HWND(hwnd)))


def is_visible(hwnd: int) -> bool:
    _require_windows()
    return bool(_user32.IsWindowVisible(wintypes.HWND(hwnd)))


def is_minimized(hwnd: int) -> bool:
    _require_windows()
    return bool(_user32.IsIconic(wintypes.HWND(hwnd)))


def foreground_hwnd() -> int:
    _require_windows()
    return int(_user32.GetForegroundWindow() or 0)


def is_foreground(hwnd: int) -> bool:
    return bool(hwnd) and foreground_hwnd() == int(hwnd)


def window_dpi(hwnd: int) -> int:
    """窗口 DPI（取不到就按 96）。"""
    _require_windows()
    try:
        value = int(_user32.GetDpiForWindow(wintypes.HWND(hwnd)))
        return value or 96
    except Exception:
        return 96


def is_window_on_screen(hwnd: int) -> bool:
    """窗口是否真的在屏幕上露出（没被完全裁到屏外；不判断是否被盖住）。"""
    try:
        if not is_visible(hwnd):
            return False
        rect = get_window_rect(hwnd)
        if rect.width <= 0 or rect.height <= 0:
            return False
        vs_x = _user32.GetSystemMetrics(76)     # SM_XVIRTUALSCREEN
        vs_y = _user32.GetSystemMetrics(77)
        vs_w = _user32.GetSystemMetrics(78)
        vs_h = _user32.GetSystemMetrics(79)
        if rect.right <= vs_x or rect.bottom <= vs_y:
            return False
        if rect.left >= vs_x + vs_w or rect.top >= vs_y + vs_h:
            return False
        return True
    except Win32Error:
        return False


def _make_fox_window(hwnd: int) -> FoxWindow:
    return FoxWindow(
        hwnd=int(hwnd),
        title=window_text(hwnd),
        pid=window_pid(hwnd),
        cls=window_class(hwnd),
        rect=get_window_rect(hwnd),
        visible=is_visible(hwnd),
        minimized=is_minimized(hwnd),
        enabled=bool(_user32.IsWindowEnabled(wintypes.HWND(hwnd))),
    )


def list_fox_windows(*, include_hidden: bool = True,
                     exe_name: str = FOX_PROCESS) -> list[FoxWindow]:
    """列出 foxwq 的**所有**顶层窗口（默认含不可见），按面积降序。

    为什么不能只看最大的：人机对弈的设置框「围棋精灵陪练」是独立
    ``#32770`` 顶层窗（NOTES.md 第 8 节），主窗口之外还有它。
    反过来也成立：实测 foxwq 3.x 的**主窗口类名本身就是 #32770**
    （2026-11-05 实测 hwnd=1508682, 2560x1531, 标题 `> [高级房1]`），
    所以千万别用「类名 == #32770」判断对话框。
    """
    _require_windows()
    pids = set(pids_of(exe_name))
    if not pids:
        return []
    out: list[FoxWindow] = []
    for hwnd in top_level_windows():
        if window_pid(hwnd) not in pids:
            continue
        win = _make_fox_window(hwnd)
        if not include_hidden and not win.visible:
            continue
        out.append(win)
    out.sort(key=lambda w: (-w.area, w.hwnd))
    return out


#: 框架/系统的辅助窗（不是给人看的界面），不参与"主窗口"评选。
#: 实测（2026-11-05，foxwq 3.x）：主窗口类名也是 ``#32770``，所以**不能**
#: 一看到 #32770 就当对话框排除；只能靠面积 + 标题 + 这个黑名单。
HELPER_WINDOW_CLASSES = frozenset({
    "MbToolTip", "tooltips_class32", "ComboLBox", "IME", "MSCTFIME UI",
    "GDI+ Hook Window Class", "MiniBlinkTimerWindowClass", "OleDdeWndClass",
    "CAsyncSocketEx Helper Window", "XHTTPWindowEx_BFSOFT_HttpWindowEx",
})
_HELPER_PREFIXES = ("CMessageWindow Window", "CGMEWindow", "XHTTPWindowEx")


def is_helper_window(win: "FoxWindow") -> bool:
    """是不是框架辅助窗（消息窗/提示窗/IME/定时器……），不是界面。"""
    cls = win.cls or ""
    if cls in HELPER_WINDOW_CLASSES:
        return True
    return any(cls.startswith(prefix) for prefix in _HELPER_PREFIXES)


def find_fox_windows(*, min_area: int = 400 * 400,
                     only_visible: bool = True,
                     exclude_helpers: bool = True,
                     exe_name: str = FOX_PROCESS) -> list[FoxWindow]:
    """候选主窗口列表（可见 + 够大），按 (是否标题像对局, 面积) 排序。

    主窗口/对局窗在**同一进程**里；对局时标题带「号房间 / 对弈中」，
    这时即使它比大厅小也优先（fox_board.py 2026-10-22 的排序规则）。
    """
    windows = list_fox_windows(include_hidden=True, exe_name=exe_name)
    hits: list[FoxWindow] = []
    for win in windows:
        if only_visible and (not win.visible or win.minimized):
            continue
        if win.area < min_area:
            continue
        if exclude_helpers and is_helper_window(win):
            continue
        hits.append(win)

    def _rank(win: FoxWindow) -> tuple[int, int, int]:
        title = win.title or ""
        playing = any(k in title for k in ("号房间", "对局", "对弈", "房间"))
        return (0 if playing else 1, -win.area, win.hwnd)

    hits.sort(key=_rank)
    return hits


def find_fox_window(*, min_area: int = 400 * 400,
                    allow_minimized: bool = False,
                    exclude_helpers: bool = True,
                    exe_name: str = FOX_PROCESS) -> FoxWindow:
    """找野狐主窗口；找不到抛 ``FoxWindowNotFound``（消息里说清原因）。"""
    _require_windows()
    all_windows = list_fox_windows(include_hidden=True, exe_name=exe_name)
    if not all_windows:
        if not pids_of(exe_name):
            raise FoxWindowNotFound(
                f"没找到野狐窗口：{exe_name} 没在运行（或没有可见窗口）")
        raise FoxWindowNotFound(
            f"foxwq.exe 在运行，但没有可用的顶层窗口（可能窗口还没建好）")

    cands = find_fox_windows(min_area=min_area, only_visible=True,
                             exclude_helpers=exclude_helpers, exe_name=exe_name)
    if not cands and allow_minimized:
        # 最小化时 GetWindowRect 是假的，尺寸滤镜不适用；按标题/面积凑合排序，
        # 调用方拿到后应 activate(allow_restore=True) 再重新 find 一次取真矩形。
        cands = sorted((w for w in all_windows if w.minimized),
                       key=lambda w: (0 if w.title else 1, -w.area, w.hwnd))
    if not cands:
        hidden = [w for w in all_windows if not w.visible]
        visible = [w for w in all_windows if w.visible]
        if hidden and not visible:
            raise FoxWindowNotFound(
                "野狐窗口被隐藏/最小化了（GetWindowRect 不可信），先让它露出来")
        if visible:
            biggest = max(visible, key=lambda w: w.area)
            raise FoxWindowNotFound(
                f"野狐没有够大的可见窗口（最大 {biggest.rect} < {min_area} px²，"
                f"class={biggest.cls!r}, title={biggest.title!r}）；"
                "可能已最小化：传 allow_minimized=True 或先还原窗口")
        raise FoxWindowNotFound("野狐没有可见窗口（可能已最小化或还在启动）")
    return cands[0]


def wait_for_fox_window(*, timeout: float = 30.0, poll: float = 0.3,
                        min_area: int = 400 * 400,
                        exe_name: str = FOX_PROCESS) -> FoxWindow:
    """等野狐主窗口出现（启动后调用）；超时抛 ``FoxWindowNotFound``。"""
    deadline = time.time() + max(0.0, float(timeout))
    last: Exception | None = None
    while True:
        try:
            return find_fox_window(min_area=min_area, exe_name=exe_name)
        except FoxWindowNotFound as exc:
            last = exc
        if time.time() >= deadline:
            raise FoxWindowNotFound(
                f"等了 {timeout:.0f}s 还是没找到野狐主窗口：{last}")
        time.sleep(max(0.05, float(poll)))


def fox_state(exe_name: str = FOX_PROCESS) -> dict[str, Any]:
    """给模型/日志看的一坨状态：进程、窗口、标题、矩形。"""
    state: dict[str, Any] = {
        "process": exe_name,
        "running": False,
        "pids": [],
        "hwnd": None,
        "title": "",
        "rect": None,
        "minimized": False,
        "foreground": False,
        "windows": [],
    }
    try:
        pids = pids_of(exe_name)
    except Win32Error:
        return state
    state["pids"] = pids
    state["running"] = bool(pids)
    if not pids:
        return state
    windows = list_fox_windows(include_hidden=True, exe_name=exe_name)
    state["windows"] = [w.to_dict() for w in windows]
    try:
        main = find_fox_window(exe_name=exe_name)
    except FoxWindowNotFound:
        return state
    state.update({
        "hwnd": main.hwnd,
        "title": main.title,
        "rect": main.rect.to_dict(),
        "minimized": main.minimized,
        "foreground": is_foreground(main.hwnd),
    })
    return state


# --------------------------------------------------------------------------
# 子控件
# --------------------------------------------------------------------------
def direct_children(parent: int) -> list[int]:
    """直接子窗口（``GW_CHILD`` + ``GW_HWNDNEXT``，不含孙子）。"""
    _require_windows()
    out: list[int] = []
    child = _user32.GetWindow(wintypes.HWND(parent), GW_CHILD)
    guard = 0
    while child and guard < 4096:
        out.append(int(child))
        child = _user32.GetWindow(wintypes.HWND(child), GW_HWNDNEXT)
        guard += 1
    return out


def _make_control(hwnd: int, depth: int, parent: int) -> Control:
    return Control(
        hwnd=int(hwnd),
        cls=window_class(hwnd),
        text=window_text(hwnd),
        rect=get_window_rect(hwnd),
        visible=is_visible(hwnd),
        enabled=bool(_user32.IsWindowEnabled(wintypes.HWND(hwnd))),
        depth=int(depth),
        parent=int(parent),
    )


def walk_controls(parent: int, *, max_depth: int = 3, max_nodes: int = 500,
                  visible_only: bool = False) -> Iterator[Control]:
    """深度优先遍历子控件（生成 ``Control``）。"""
    _require_windows()
    seen: set[int] = set()

    stack: list[tuple[int, int]] = [(hwnd, 1) for hwnd in reversed(direct_children(parent))]
    nodes = 0
    while stack and nodes < max_nodes:
        hwnd, depth = stack.pop()
        if hwnd in seen:
            continue
        seen.add(hwnd)
        nodes += 1
        ctrl = _make_control(hwnd, depth, parent)
        if not visible_only or ctrl.visible:
            yield ctrl
        if depth < max_depth:
            for child in reversed(direct_children(hwnd)):
                stack.append((child, depth + 1))


def enum_children(parent: int, *, max_depth: int = 3, max_nodes: int = 500,
                  visible_only: bool = False) -> list[Control]:
    """枚举子控件（默认最多 3 层、500 个）。"""
    return list(walk_controls(parent, max_depth=max_depth,
                              max_nodes=max_nodes, visible_only=visible_only))


def find_controls(parent: int, *, cls: str | None = None,
                  text: str | None = None, contains: str | None = None,
                  max_depth: int = 4, visible_only: bool = False,
                  limit: int = 50) -> list[Control]:
    """按类名/文本精确或包含匹配找控件。"""
    hits: list[Control] = []
    for ctrl in walk_controls(parent, max_depth=max_depth,
                              visible_only=visible_only):
        if cls is not None and ctrl.cls.lower() != str(cls).lower():
            continue
        if text is not None and ctrl.text != text:
            continue
        if contains is not None and str(contains) not in (ctrl.text or ""):
            continue
        hits.append(ctrl)
        if len(hits) >= limit:
            break
    return hits


def find_control(parent: int, **kwargs: Any) -> Control | None:
    """``find_controls`` 的第一个；没有返回 None。"""
    hits = find_controls(parent, limit=1, **kwargs)
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# 坐标点下的窗口
# --------------------------------------------------------------------------
def window_from_point(x: int, y: int) -> int:
    """屏幕点 (x,y) 命中的最上层窗口（可能是别的程序，比如桌宠）。"""
    _require_windows()
    return int(_user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0)


def deepest_child_at(hwnd: int, x: int, y: int) -> int:
    """在 hwnd 内部一路往下找屏幕点 (x,y) 命中的最深层子控件。"""
    _require_windows()
    current = int(hwnd)
    flags = CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT
    for _ in range(12):
        cx, cy = screen_to_client(current, x, y)
        child = _user32.ChildWindowFromPointEx(
            wintypes.HWND(current), wintypes.POINT(int(cx), int(cy)), flags)
        child = int(child or 0)
        if not child or child == current:
            break
        current = child
    return current


def window_at_point(x: int, y: int) -> dict[str, Any]:
    """调试用：屏幕点下的窗口是谁（含 PID/类名/标题/矩形）。"""
    hwnd = window_from_point(x, y)
    if not hwnd:
        return {"hwnd": 0, "pid": 0, "class": "", "text": "", "rect": None}
    return {
        "hwnd": hwnd,
        "pid": window_pid(hwnd),
        "class": window_class(hwnd),
        "text": window_text(hwnd),
        "rect": get_window_rect(hwnd).to_dict(),
    }


def describe_point(x: int, y: int, *, depth: int = 6) -> dict[str, Any]:
    """调试用：屏幕点 + 从桌面窗口往下钻到的控件链（probe_ui 的简版）。"""
    top = window_from_point(x, y)
    chain: list[dict[str, Any]] = []
    if top:
        chain.append({"hwnd": top, "class": window_class(top),
                      "text": window_text(top),
                      "rect": get_window_rect(top).to_dict()})
        current = top
        for _ in range(max(0, depth - 1)):
            cx, cy = screen_to_client(current, x, y)
            child = int(_user32.ChildWindowFromPointEx(
                wintypes.HWND(current), wintypes.POINT(int(cx), int(cy)),
                CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT) or 0)
            if not child or child == current:
                break
            chain.append({"hwnd": child, "class": window_class(child),
                          "text": window_text(child),
                          "rect": get_window_rect(child).to_dict()})
            current = child
    return {"x": int(x), "y": int(y), "top": chain[0] if chain else None,
            "chain": chain, "deepest": chain[-1] if chain else None}


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------
_DPI_STATE = ""


def enable_dpi_awareness() -> bool:
    """把当前进程设成 DPI 感知（幂等）。

    不设的后果：2560 的屏被虚拟化成 1707，PrintWindow/GetWindowRect 和
    截图对不上 1.5 倍 —— 坐标全错（fox_board.py 顶部注释同款教训）。
    """
    global _DPI_STATE
    if not IS_WINDOWS:
        return False
    if _DPI_STATE:
        return _DPI_STATE == "ok"
    if os.environ.get("DSH_FOX_NO_DPI"):
        _DPI_STATE = "skipped"
        return False
    try:
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_STATE = "ok"
            return True
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(2) == 0:      # PER_MONITOR_DPI_AWARE
            _DPI_STATE = "ok"
            return True
    except Exception:
        pass
    try:
        if _user32.SetProcessDPIAware():
            _DPI_STATE = "ok"
            return True
    except Exception:
        pass
    _DPI_STATE = "failed"
    return False


# --------------------------------------------------------------------------
# 窗口激活 / 显示
# --------------------------------------------------------------------------
def show_window(hwnd: int, cmd: int = SW_SHOW) -> bool:
    _require_windows()
    return bool(_user32.ShowWindow(wintypes.HWND(hwnd), int(cmd)))


def bring_to_top(hwnd: int) -> None:
    """硬推到 z 序最上层（SetForegroundWindow 对后台进程有限制时的补充）。"""
    _require_windows()
    HWND_TOP = 0
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    try:
        _user32.SetWindowPos(wintypes.HWND(hwnd), HWND_TOP, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    except Win32Error:
        pass


def activate(hwnd: int, *, allow_restore: bool = True) -> bool:
    """把窗口提到前台，返回 ``True`` = 它现在确实是前台（可以安全点鼠标）。

    ``allow_restore=False`` 时最小化窗口**绝不还原**（用户最小化 = 明确意图），
    直接返回 False 让上层等（fox_board.py 2026-10-23 的教训）。

    后台进程抢前台会被 Windows 前台锁挡住；先 ``SetForegroundWindow``，
    不行再挂到当前前台线程的输入队列上重试（系统留的合法通道）。
    """
    _require_windows()
    hwnd = int(hwnd)
    if not is_window(hwnd):
        return False
    try:
        if is_minimized(hwnd):
            if not allow_restore:
                return False
            _user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
            time.sleep(0.25)
        _user32.SetForegroundWindow(wintypes.HWND(hwnd))
        time.sleep(0.12)
        if is_foreground(hwnd):
            bring_to_top(hwnd)
            return True
        fg = foreground_hwnd()
        if fg:
            tid_cur = int(_kernel32.GetCurrentThreadId())
            tid_fg = int(_user32.GetWindowThreadProcessId(
                wintypes.HWND(fg), None) or 0)
            attached = False
            try:
                if tid_fg and tid_fg != tid_cur:
                    attached = bool(_user32.AttachThreadInput(
                        wintypes.DWORD(tid_cur), wintypes.DWORD(tid_fg), True))
                bring_to_top(hwnd)
                _user32.SetForegroundWindow(wintypes.HWND(hwnd))
            finally:
                if attached:
                    _user32.AttachThreadInput(
                        wintypes.DWORD(tid_cur), wintypes.DWORD(tid_fg), False)
            time.sleep(0.12)
    except Win32Error:
        pass
    return is_foreground(hwnd)


# --------------------------------------------------------------------------
# 鼠标
# --------------------------------------------------------------------------
_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click_at(x: int, y: int, *, button: str = "left", double: bool = False,
             move_delay: float = 0.012, down_delay: float = 0.012,
             after: float = 0.03) -> tuple[int, int]:
    """普通鼠标点击（SetCursorPos + mouse_event），返回点击的屏幕坐标。

    ⚠️ 打的是屏幕绝对坐标，不认窗口层级；野狐被盖住/最小化时不能这么点
    （``actions`` 会先看前台，否则改走 ``post_click``）。
    """
    _require_windows()
    button = (button or "left").lower()
    if button not in _BUTTONS:
        raise Win32Error(f"不支持的鼠标键：{button!r}（left/right/middle）")
    down, up = _BUTTONS[button]
    sx, sy = int(round(x)), int(round(y))
    if not _user32.SetCursorPos(sx, sy):
        raise Win32Error(f"SetCursorPos({sx},{sy}) 失败")
    time.sleep(max(0.0, move_delay))
    clicks = 2 if double else 1
    for index in range(clicks):
        _user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(max(0.0, down_delay))
        _user32.mouse_event(up, 0, 0, 0, 0)
        if index + 1 < clicks:
            time.sleep(0.05)
    if after > 0:
        time.sleep(after)
    return sx, sy


def double_click_at(x: int, y: int, **kwargs: Any) -> tuple[int, int]:
    kwargs.setdefault("double", True)
    return click_at(x, y, **kwargs)


def right_click_at(x: int, y: int, **kwargs: Any) -> tuple[int, int]:
    kwargs.setdefault("button", "right")
    return click_at(x, y, **kwargs)


def scroll_at(x: int, y: int, notches: int, *, horizontal: bool = False,
              move_first: bool = True, after: float = 0.05) -> None:
    """在 (x,y) 滚轮；``notches`` 正数向下/向右，负数向上/向左。"""
    _require_windows()
    if move_first:
        _user32.SetCursorPos(int(x), int(y))
        time.sleep(0.012)
    delta = int(notches) * 120
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _user32.mouse_event(flag, 0, 0, ctypes.c_ulong(delta & 0xFFFFFFFF).value, 0)
    if after > 0:
        time.sleep(after)


def post_click(hwnd: int, x: int, y: int, *, child: bool = True,
               button: str = "left", double: bool = False,
               delay: float = 0.05) -> int:
    """把点击 PostMessage 到窗口/最深子控件（绕开桌宠等遮挡），返回目标 hwnd。

    (x, y) 是**屏幕物理坐标**；内部用 ``ScreenToClient`` 换成目标窗口客户区坐标。
    注意：PostMessage 不移动真实鼠标，某些自绘控件可能不响应；能正常点就用
    ``click_at``，被挡住再走这里（DESIGN 5.3）。
    """
    _require_windows()
    button = (button or "left").lower()
    if button not in _BUTTONS:
        raise Win32Error(f"不支持的鼠标键：{button!r}")
    target_hwnd = int(hwnd)
    if not is_window(target_hwnd):
        raise Win32Error(f"post_click: 目标窗口无效 hwnd={target_hwnd}")
    if is_minimized(target_hwnd):
        # 最小化时客户区坐标/消息都没意义，别假装点成功了
        raise Win32Error(f"窗口已最小化（hwnd={target_hwnd}），PostMessage 点击无效")
    if button == "left":
        down, up = WM_LBUTTONDOWN, WM_LBUTTONUP
    elif button == "right":
        down, up = WM_RBUTTONDOWN, WM_RBUTTONUP
    else:
        down, up = 0x0207, 0x0208        # WM_MBUTTONDOWN/UP
    target = deepest_child_at(hwnd, x, y) if child else int(hwnd)
    if not is_window(target):
        raise Win32Error(f"post_click: 目标窗口无效 hwnd={target}")
    cx, cy = screen_to_client(target, x, y)
    lparam = make_lparam(cx, cy)
    h = wintypes.HWND(target)
    held = {"left": MK_LBUTTON, "right": MK_RBUTTON, "middle": MK_MBUTTON}[button]
    _user32.PostMessageW(h, WM_MOUSEMOVE, 0, lparam)
    time.sleep(max(0.0, delay))
    if double:
        _user32.PostMessageW(h, WM_LBUTTONDBLCLK, held, lparam)
        time.sleep(max(0.0, delay))
    _user32.PostMessageW(h, down, held, lparam)
    time.sleep(max(0.0, delay))
    _user32.PostMessageW(h, up, 0, lparam)
    return target


def bm_click(hwnd: int) -> bool:
    """``BM_CLICK`` 点按钮（登录框协议框/登录按钮实测有效）。"""
    _require_windows()
    result = _user32.SendMessageW(wintypes.HWND(int(hwnd)), BM_CLICK, 0, 0)
    return bool(result)


# --------------------------------------------------------------------------
# 键盘
# --------------------------------------------------------------------------
def _key_input(vk: int, *, up: bool = False, unicode_scan: int | None = None) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    ki = item.union.ki
    if unicode_scan is not None:
        ki.wVk = 0
        ki.wScan = unicode_scan & 0xFFFF
        ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    else:
        ki.wVk = vk & 0xFFFF
        ki.wScan = 0
        ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    ki.time = 0
    ki.dwExtraInfo = 0
    return item


def _send_inputs(items: Sequence[INPUT]) -> int:
    if not items:
        return 0
    array_type = INPUT * len(items)
    array = array_type(*items)
    sent = int(_user32.SendInput(len(items), array, ctypes.sizeof(INPUT)))
    if sent != len(items):
        raise Win32Error(
            f"SendInput 只送进去 {sent}/{len(items)} 个事件"
            f"（可能是 UIPI 拦截：本进程完整性级别低于目标）")
    return sent


def send_unicode_text(text: str, *, interval: float = 0.008,
                      chunk: int = 16, sleep: Callable[[float], None] = time.sleep) -> int:
    """用 ``SendInput + KEYEVENTF_UNICODE`` 输入任意文字（不依赖输入法）。

    中文、emoji 都能过；emoji 走代理对，按 UTF-16 码元拆成两次事件。
    返回发出的字符数（码点数）。
    """
    _require_windows()
    if not isinstance(text, str) or not text:
        return 0
    units = text.encode("utf-16-le")
    scans = [units[i] | (units[i + 1] << 8) for i in range(0, len(units), 2)]
    count = 0
    for start in range(0, len(scans), max(1, int(chunk))):
        batch: list[INPUT] = []
        for scan in scans[start:start + max(1, int(chunk))]:
            batch.append(_key_input(0, unicode_scan=scan))
            batch.append(_key_input(0, up=True, unicode_scan=scan))
        _send_inputs(batch)
        count += len(batch) // 2
        if interval > 0:
            sleep(interval)
    return count


def send_key(name: str, *, modifiers: Iterable[str] = (),
             interval: float = 0.02, sleep: Callable[[float], None] = time.sleep) -> str:
    """按一个键（可带修饰键）。``name`` 支持 ``"Enter"`` / ``"ctrl+a"`` 两种写法。

    返回规范名（例如 ``"ctrl+a"``），方便记 trace。
    """
    _require_windows()
    parsed_mods, vk, display = normalize_key(name)
    mods = list(parsed_mods)
    for extra in modifiers or ():
        low = str(extra).strip().lower()
        mod = _MODIFIER_NAMES.get(low, low)
        if mod not in _MODIFIER_VK:
            raise ValueError(f"不认识的修饰键：{extra!r}")
        if mod not in mods:
            mods.append(mod)
    display = ("+".join(mods + [display.split("+")[-1]]) if mods else display)
    batch: list[INPUT] = [_key_input(_MODIFIER_VK[mod]) for mod in mods]
    batch.append(_key_input(vk))
    batch.append(_key_input(vk, up=True))
    batch.extend(_key_input(_MODIFIER_VK[mod], up=True) for mod in reversed(mods))
    _send_inputs(batch)
    if interval > 0:
        sleep(interval)
    return display


def send_keys(keys: Sequence[str], *, interval: float = 0.05,
              sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """依次按多个键。"""
    sent: list[str] = []
    for name in keys:
        sent.append(send_key(name, interval=interval, sleep=sleep))
    return sent


# 设一次 DPI 感知：模块 import 时自保（和 fox_board.py 顶部同理）。
# 设过的进程再设会返回失败，这里吞掉，顺序无关。
if IS_WINDOWS:
    try:
        enable_dpi_awareness()
    except Exception:      # pragma: no cover - 任何环境问题都不该让 import 失败
        pass
