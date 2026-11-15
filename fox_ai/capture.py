# -*- coding: utf-8 -*-
"""野狐窗口截图：``PrintWindow`` 优先，``mss`` 兜底。

为什么是这个顺序
----------------
PrintWindow(``PW_RENDERFULLCONTENT``) 让窗口自己重绘一份 —— 被桌宠
（Coopanion.exe）盖住、失焦都能截到，而且**不抢焦点、不改 z 序**，
老板在别的窗口打字也不会被弄丢光标（fox_board.py 2026-10-23 的教训）。

少数 GDI 程序 PrintWindow 返回全黑，那时才退到 ``mss`` 屏幕截取。
退路要求窗口真的露在屏幕上，而且**被遮挡时截到的是遮挡物**——
所以 ``Shot.method`` 会如实写 ``"mss"``，调用方（trace）能看到。

坐标约定
--------
``Shot`` 里的 ``(x, y)`` 是**截图图像像素**；模型输出的也是这个。
``shot.to_screen(x, y)`` 换到屏幕物理像素：
``left + x * scale, top + y * scale``。
``scale`` = 物理 px / 图像 px，PrintWindow/mss 正常都是 1.0；
将来要是缩了图（annotate 会缩），换算照样对。

用法::

    from agent import capture

    shot = capture.grab_fox()                 # 主窗口
    shot = capture.grab_fox(hwnd=other_hwnd)  # 指定窗口（比如 #32770 对话框）
    sx, sy = shot.to_screen(812, 340)
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from . import win32

__all__ = [
    "CaptureError",
    "Shot",
    "grab_fox",
    "grab_window",
    "grab_rect",
    "print_window_image",
    "grab_screen_rect",
]


class CaptureError(RuntimeError):
    """截不到图（窗口没了 / 最小化 / PrintWindow 和 mss 都失败）。"""


# --------------------------------------------------------------------------
# Shot
# --------------------------------------------------------------------------
@dataclass
class Shot:
    """一张截图 + 它和屏幕的换算关系。"""

    image: Image.Image
    origin: tuple[int, int] = (0, 0)
    scale: float = 1.0
    hwnd: int = 0
    title: str = ""
    rect: Any = None            # win32.Rect（或 (left, top, w, h)）
    method: str = ""            # "printwindow" / "mss"
    dpi: int = 96
    captured_at: float = field(default_factory=time.time)

    # -- 尺寸 ---------------------------------------------------------
    @property
    def size(self) -> tuple[int, int]:
        return tuple(self.image.size)      # type: ignore[return-value]

    @property
    def width(self) -> int:
        return int(self.image.size[0])

    @property
    def height(self) -> int:
        return int(self.image.size[1])

    # -- 坐标换算 -----------------------------------------------------
    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """截图图像坐标 → 屏幕物理坐标。"""
        return (int(round(self.origin[0] + float(x) * float(self.scale))),
                int(round(self.origin[1] + float(y) * float(self.scale))))

    def to_image(self, screen_x: float, screen_y: float) -> tuple[int, int]:
        """屏幕物理坐标 → 截图图像坐标。"""
        scale = float(self.scale) or 1.0
        return (int(round((float(screen_x) - self.origin[0]) / scale)),
                int(round((float(screen_y) - self.origin[1]) / scale)))

    def contains(self, x: float, y: float, margin: int = 0) -> bool:
        return (-margin <= x <= self.width + margin
                and -margin <= y <= self.height + margin)

    # -- 变换 ---------------------------------------------------------
    def crop(self, box: Iterable[int]) -> "Shot":
        """按**图像坐标**裁剪（``(left, top, right, bottom)``），origin/scale 跟着改。"""
        left, top, right, bottom = (int(v) for v in tuple(box)[:4])
        left = max(0, left)
        top = max(0, top)
        right = min(self.width, right)
        bottom = min(self.height, bottom)
        if right <= left or bottom <= top:
            raise CaptureError(f"裁剪区域非法：{box}")
        image = self.image.crop((left, top, right, bottom))
        origin = self.to_screen(left, top)
        return Shot(image=image, origin=origin, scale=self.scale,
                    hwnd=self.hwnd, title=self.title, rect=self.rect,
                    method=self.method, dpi=self.dpi,
                    captured_at=self.captured_at)

    def save(self, path: str | Path, *, quality: int = 85) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if quality is not None:
            self.image.convert("RGB").save(target, "JPEG", quality=int(quality))
        else:
            self.image.save(target)
        return target

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "method": self.method,
            "size": [self.width, self.height],
            "origin": [int(self.origin[0]), int(self.origin[1])],
            "scale": round(float(self.scale), 6),
            "dpi": self.dpi,
        }

    def __repr__(self) -> str:
        return (f"Shot({self.width}x{self.height} @ {self.origin}, "
                f"scale={self.scale:.4f}, method={self.method!r}, "
                f"title={self.title!r})")


# --------------------------------------------------------------------------
# Win32/GDI 打印窗口
# --------------------------------------------------------------------------
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


_GDI_READY = False
_user32: Any = None
_gdi32: Any = None


def _setup_gdi() -> bool:
    global _GDI_READY, _user32, _gdi32
    if _GDI_READY:
        return _user32 is not None
    _GDI_READY = True
    if not win32.IS_WINDOWS:
        return False
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    except OSError:
        _user32 = _gdi32 = None
        return False
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _user32.PrintWindow.restype = wintypes.BOOL
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                 wintypes.UINT, ctypes.c_void_p,
                                 ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT]
    _gdi32.GetDIBits.restype = ctypes.c_int
    return True


def _looks_valid(image: Image.Image | None) -> bool:
    """PrintWindow 偶尔返回全黑/全白 —— 用极差粗筛，避免把黑图当成功。"""
    if image is None:
        return False
    try:
        if image.width < 8 or image.height < 8:
            return False
        rgb = image.convert("RGB")
        extrema = rgb.getextrema()
        # getextrema 对 RGB 返回 ((rmin,rmax),(gmin,gmax),(bmin,bmax))
        spans = [hi - lo for lo, hi in extrema]
        return max(spans) >= 3
    except Exception:
        return False


def print_window_image(hwnd: int, width: int | None = None,
                       height: int | None = None,
                       *, flags: int = win32.PW_RENDERFULLCONTENT) -> Image.Image | None:
    """用 PrintWindow 把窗口画进内存位图；失败/全黑返回 ``None``。

    ``flags``：先 ``PW_RENDERFULLCONTENT(2)``（Win8.1+，能画含子窗口的界面），
    不行退 ``0``。
    """
    if not win32.IS_WINDOWS or not hwnd:
        return None
    if not _setup_gdi() or _user32 is None or _gdi32 is None:
        return None
    if width is None or height is None:
        rect = win32.get_window_rect(hwnd)
        width, height = rect.width, rect.height
    width, height = int(width), int(height)
    if width < 8 or height < 8:
        return None

    screen_dc = _user32.GetDC(0)
    mem_dc = None
    bitmap = None
    old = None
    try:
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        if not mem_dc:
            return None
        bitmap = _gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            return None
        old = _gdi32.SelectObject(mem_dc, bitmap)
        ok = _user32.PrintWindow(wintypes.HWND(int(hwnd)), mem_dc, int(flags))
        if not ok and flags:
            ok = _user32.PrintWindow(wintypes.HWND(int(hwnd)), mem_dc, 0)
        if not ok:
            return None

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height          # 负 = 自上而下，省一次翻转
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0           # BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        got = _gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                               ctypes.byref(header), 0)
        if not got:
            return None
        image = Image.frombuffer("RGBA", (width, height), buffer,
                                 "raw", "BGRA", 0, 1).convert("RGB")
        return image
    except Exception:
        return None
    finally:
        try:
            if mem_dc and old:
                _gdi32.SelectObject(mem_dc, old)
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if mem_dc:
                _gdi32.DeleteDC(mem_dc)
            if screen_dc:
                _user32.ReleaseDC(0, screen_dc)
        except Exception:
            pass


# --------------------------------------------------------------------------
# mss 兜底
# --------------------------------------------------------------------------
def grab_screen_rect(left: int, top: int, width: int, height: int) -> Image.Image:
    """屏幕矩形截取（mss）。失败抛 ``CaptureError``。"""
    left, top, width, height = int(left), int(top), int(width), int(height)
    if width < 8 or height < 8:
        raise CaptureError(f"截取区域太小：{width}x{height}")
    try:
        import mss  # 延迟导入：没装 mss 也能用 PrintWindow 那条路
    except ImportError as exc:
        raise CaptureError("没装 mss，且 PrintWindow 不可用（pip install mss）") from exc
    try:
        with mss.mss() as sct:
            grab = sct.grab({"left": left, "top": top,
                             "width": width, "height": height})
            return Image.frombytes("RGB", grab.size, grab.rgb)
    except Exception as exc:
        raise CaptureError(f"mss 截屏失败 ({left},{top},{width}x{height})：{exc}") from exc


# --------------------------------------------------------------------------
# 抓图入口
# --------------------------------------------------------------------------
def _resolve_window(hwnd: int | None, *, min_area: int) -> win32.FoxWindow:
    if hwnd is None:
        return win32.find_fox_window(min_area=min_area)   # 自带清晰错误
    if not win32.is_window(int(hwnd)):
        raise CaptureError(f"窗口已消失 hwnd={hwnd}")
    return win32.FoxWindow(
        hwnd=int(hwnd),
        title=win32.window_text(int(hwnd)),
        pid=win32.window_pid(int(hwnd)),
        cls=win32.window_class(int(hwnd)),
        rect=win32.get_window_rect(int(hwnd)),
        visible=win32.is_visible(int(hwnd)),
        minimized=win32.is_minimized(int(hwnd)),
    )


def grab_window(hwnd: int, *, allow_restore: bool = False,
                prefer: str = "printwindow") -> Shot:
    """截指定窗口（PrintWindow 优先 / mss 兜底）。"""
    win = _resolve_window(hwnd, min_area=0)
    return _grab(win, allow_restore=allow_restore, prefer=prefer)


def grab_fox(hwnd: int | None = None, *, min_area: int = 400 * 400,
             allow_restore: bool = False,
             prefer: str = "printwindow") -> Shot:
    """截野狐主窗口（或 ``hwnd`` 指定的那个）。

    - 窗口已最小化：**默认不还原**（用户最小化 = 明确意图），抛 ``CaptureError``；
      显式 ``allow_restore=True`` 才 ``ShowWindow(SW_RESTORE)``。
    - PrintWindow 成功 → ``method="printwindow"``；
      失败/全黑 → mss（要求窗口露在屏幕上）→ ``method="mss"``。
    """
    win = _resolve_window(hwnd, min_area=min_area)
    return _grab(win, allow_restore=allow_restore, prefer=prefer)


def _grab(win: win32.FoxWindow, *, allow_restore: bool,
          prefer: str) -> Shot:
    if prefer not in ("printwindow", "mss", "auto"):
        raise CaptureError(f"prefer 只能是 printwindow/mss/auto，收到 {prefer!r}")
    if win.minimized:
        if not allow_restore:
            raise CaptureError(
                f"野狐窗口已最小化（{win.title!r}）：先让它露出来，"
                "或 grab_fox(allow_restore=True)")
        win32.activate(win.hwnd, allow_restore=True)
        time.sleep(0.25)
        win = _resolve_window(win.hwnd, min_area=0)
        if win.minimized:
            raise CaptureError(f"窗口还是最小化，截不了图 hwnd={win.hwnd}")

    rect = win.rect
    if rect.width < 8 or rect.height < 8:
        raise CaptureError(
            f"窗口矩形不可用（{rect}）：野狐可能刚启动还没画出来，等 0.3s 再试")

    problems: list[str] = []
    if prefer in ("printwindow", "auto"):
        image = print_window_image(win.hwnd, rect.width, rect.height)
        if _looks_valid(image):
            return _make_shot(image, win, rect, "printwindow")   # type: ignore[arg-type]
        problems.append("PrintWindow 失败或全黑")

    if prefer in ("mss", "auto") or problems:
        if not win32.is_window_on_screen(win.hwnd):
            problems.append("窗口不在屏幕上，mss 截不到")
        else:
            try:
                image = grab_screen_rect(rect.left, rect.top, rect.width, rect.height)
            except CaptureError as exc:
                problems.append(str(exc))
            else:
                if _looks_valid(image):
                    return _make_shot(image, win, rect, "mss")
                problems.append("mss 截到的是纯色图（窗口可能被完全遮挡）")

    raise CaptureError(
        "截取野狐窗口失败：" + "；".join(problems or ["未知原因"])
        + f"（hwnd={win.hwnd}, rect={rect}, title={win.title!r}）")


def _make_shot(image: Image.Image, win: win32.FoxWindow,
               rect: win32.Rect, method: str) -> Shot:
    scale = (rect.width / image.width) if image.width else 1.0
    if not scale or scale <= 0:
        scale = 1.0
    return Shot(
        image=image,
        origin=(int(rect.left), int(rect.top)),
        scale=float(scale),
        hwnd=int(win.hwnd),
        title=win.title,
        rect=rect,
        method=method,
        dpi=win32.window_dpi(win.hwnd),
    )


def grab_rect(rect: Any, *, scale: float = 1.0) -> Shot:
    """直接截屏幕矩形（调试/兜底用）。``rect`` 可以是 ``win32.Rect`` 或四元组。"""
    if isinstance(rect, win32.Rect):
        left, top, width, height = rect.as_tuple()
    else:
        left, top, width, height = (int(v) for v in tuple(rect)[:4])
    image = grab_screen_rect(left, top, width, height)
    return Shot(image=image, origin=(left, top), scale=float(scale or 1.0),
                method="mss")
