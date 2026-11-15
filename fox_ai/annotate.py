# -*- coding: utf-8 -*-
"""截图 → 模型友好的标注图（坐标网格 + 标签，JPEG）。

为什么要格子
------------
模型给的是像素坐标，图上一旦没有刻度，它就只能瞎猜「大概在右下」。
每 100px 一条线 + 坐标数字，点击精度肉眼可见地提高；同时把最长边压到
1600 以内，免得一张 1707x1021 的 PNG 把 token 和带宽吃光。

坐标系（重要）
--------------
- 标注图可能被缩小：``Annotation.scale`` = **物理像素 / 标注图像素**，
  ``Annotation.to_screen(x, y)`` 直接给出屏幕物理坐标 —— 模型输出
  「标注图里的 (x,y)」时，actions 拿它换算就行。
- 图上的数字标签是**标注图像素坐标**，和模型看到的像素一一对应。

用法::

    from agent import capture, annotate

    shot = capture.grab_fox()
    ann = annotate.grid(shot, out_path=trace.screens_dir)  # 返回路径 + base64
    ann.path, ann.b64, ann.jpeg
    sx, sy = ann.to_screen(812, 340)
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "MAX_SIDE",
    "DEFAULT_STEP",
    "DEFAULT_QUALITY",
    "Annotation",
    "grid",
    "annotate",
    "encode_jpeg",
    "font_path",
    "grid_positions",
]

MAX_SIDE = 1600            # 长边上限（DESIGN 5.1）
DEFAULT_STEP = 100         # 网格间距（标注图像素）
DEFAULT_QUALITY = 85       # JPEG 质量

# 中文字体优先级；全是 Windows 自带，找不到就 PIL 默认（数字照样能画）
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
    r"C:\Windows\Fonts\deng.ttf",
    r"/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

GRID_COLOR = (0, 255, 120, 95)          # 半透明荧光绿，压得住野狐的深色背景
AXIS_COLOR = (0, 255, 120, 160)
LABEL_COLOR = (0, 255, 120, 255)
LABEL_OUTLINE = (0, 0, 0, 220)

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_FONT_PATH: str | None | bool = None     # None=没算过，False=没有可用字体


def font_path() -> str | None:
    """找一款可用的字体（优先微软雅黑 msyh.ttc）。"""
    global _FONT_PATH
    if _FONT_PATH is not None:
        return _FONT_PATH or None
    for candidate in FONT_CANDIDATES:
        try:
            if os.path.exists(candidate):
                _FONT_PATH = candidate
                return candidate
        except OSError:
            continue
    _FONT_PATH = False
    return None


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size = max(8, int(size))
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    path = font_path()
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            font = ImageFont.load_default()
    else:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def grid_positions(length: int, step: int = DEFAULT_STEP) -> list[int]:
    """``0, step, 2*step, ..., < length``（含 0，不含末端，避免贴边）。"""
    step = max(1, int(step))
    return list(range(0, max(0, int(length)), step))


def encode_jpeg(image: Image.Image, quality: int = DEFAULT_QUALITY) -> bytes:
    """PIL Image → JPEG bytes。"""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=int(quality), optimize=True)
    return buffer.getvalue()


@dataclass
class Annotation:
    """标注结果：图 + 到屏幕的换算 + 落盘路径 + base64。"""

    image: Image.Image
    shot: Any = None
    path: str = ""
    jpeg: bytes = b""
    grid_step: int = DEFAULT_STEP
    origin: tuple[int, int] = (0, 0)
    scale: float = 1.0          # 物理像素 / 标注图像素
    quality: int = DEFAULT_QUALITY
    created_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

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

    # -- 换算 ---------------------------------------------------------
    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """标注图像素坐标 → 屏幕物理坐标。"""
        return (int(round(self.origin[0] + float(x) * float(self.scale))),
                int(round(self.origin[1] + float(y) * float(self.scale))))

    def to_image(self, screen_x: float, screen_y: float) -> tuple[int, int]:
        scale = float(self.scale) or 1.0
        return (int(round((float(screen_x) - self.origin[0]) / scale)),
                int(round((float(screen_y) - self.origin[1]) / scale)))

    def contains(self, x: float, y: float, margin: int = 0) -> bool:
        return (-margin <= x <= self.width + margin
                and -margin <= y <= self.height + margin)

    # -- base64 / data URI -------------------------------------------
    @property
    def b64(self) -> str:
        """JPEG 的 base64 文本（ASCII）。"""
        if self.jpeg:
            return base64.b64encode(self.jpeg).decode("ascii")
        if self.path and os.path.exists(self.path):
            with open(self.path, "rb") as fp:
                return base64.b64encode(fp.read()).decode("ascii")
        return ""

    @property
    def b64_bytes(self) -> bytes:
        return self.b64.encode("ascii")

    def data_uri(self, mime: str = "image/jpeg") -> str:
        """``data:image/jpeg;base64,...``（llm 客户端也认这个格式）。"""
        return f"data:{mime};base64,{self.b64}"

    # -- 落盘 ---------------------------------------------------------
    def save(self, path: str | os.PathLike[str], *,
             quality: int | None = None) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.jpeg
        if quality is not None and int(quality) != int(self.quality):
            payload = encode_jpeg(self.image, int(quality))
            self.jpeg = payload
            self.quality = int(quality)
        target.write_bytes(payload or encode_jpeg(self.image, self.quality))
        self.path = str(target)
        return target

    def to_dict(self) -> dict[str, Any]:
        meta = {
            "size": [self.width, self.height],
            "origin": [int(self.origin[0]), int(self.origin[1])],
            "scale": round(float(self.scale), 6),
            "grid": int(self.grid_step),
            "quality": int(self.quality),
            "jpeg_bytes": len(self.jpeg),
            "path": self.path,
        }
        meta.update(self.meta)
        return meta

    def __repr__(self) -> str:
        return (f"Annotation({self.width}x{self.height}, scale={self.scale:.4f}, "
                f"grid={self.grid_step}, jpeg={len(self.jpeg)}B, path={self.path!r})")


def _source_image(source: Any) -> tuple[Image.Image, tuple[int, int], float, Any]:
    """从 ``Shot`` / ``Annotation`` / PIL Image 里取出 (图, origin, scale, 源对象)。"""
    if isinstance(source, Image.Image):
        return source.convert("RGB"), (0, 0), 1.0, None
    image = getattr(source, "image", None)
    if isinstance(image, Image.Image):
        origin = tuple(getattr(source, "origin", (0, 0)) or (0, 0))
        try:
            scale = float(getattr(source, "scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        return image.convert("RGB"), (int(origin[0]), int(origin[1])), scale, source
    raise TypeError(
        f"annotate.grid 需要 Shot/Annotation/PIL.Image，收到 {type(source).__name__}")


def _resolve_out_path(out_path: Any) -> Path:
    """``out_path`` 可以是文件、目录，或不给（落到临时文件）。"""
    if out_path is None:
        fd, name = tempfile.mkstemp(prefix="fox-annot-", suffix=".jpg")
        os.close(fd)
        return Path(name)
    path = Path(out_path)
    if path.is_dir() or str(out_path).endswith(os.sep):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        candidate = path / f"annot-{stamp}.jpg"
        index = 1
        while candidate.exists():
            candidate = path / f"annot-{stamp}-{index:02d}.jpg"
            index += 1
        path = candidate
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _draw_grid(image: Image.Image, *, step: int, labels: bool,
               crosshair: tuple[float, float] | None,
               font_size: int) -> Image.Image:
    """在图上画网格 + 标签（返回新的 RGB 图）。"""
    width, height = image.size
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for x in grid_positions(width, step):
        color = AXIS_COLOR if x == 0 else GRID_COLOR
        draw.line([(x, 0), (x, height - 1)], fill=color, width=1)
    for y in grid_positions(height, step):
        color = AXIS_COLOR if y == 0 else GRID_COLOR
        draw.line([(0, y), (width - 1, y)], fill=color, width=1)

    if labels:
        font = _load_font(font_size)
        for x in grid_positions(width, step):
            if x == 0:
                continue
            _draw_label(draw, (x + 2, 1), str(x), font)
        for y in grid_positions(height, step):
            if y == 0:
                continue
            _draw_label(draw, (2, y + 1), str(y), font)

    if crosshair is not None:
        cx, cy = float(crosshair[0]), float(crosshair[1])
        draw.line([(cx, 0), (cx, height - 1)], fill=(255, 80, 80, 220), width=2)
        draw.line([(0, cy), (width - 1, cy)], fill=(255, 80, 80, 220), width=2)
        draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10],
                     outline=(255, 80, 80, 255), width=2)

    merged = Image.alpha_composite(image.convert("RGBA"), overlay)
    return merged.convert("RGB")


def _draw_label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
                font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> None:
    try:
        draw.text(xy, text, font=font, fill=LABEL_COLOR,
                  stroke_width=2, stroke_fill=LABEL_OUTLINE)
    except TypeError:       # 老 Pillow 不认 stroke_*
        draw.text(xy, text, font=font, fill=LABEL_COLOR)


def grid(source: Any, *, out_path: Any = None,
         step: int = DEFAULT_STEP, max_side: int = MAX_SIDE,
         quality: int = DEFAULT_QUALITY, labels: bool = True,
         max_width: int | None = None,
         crosshair: tuple[float, float] | None = None,
         font_size: int | None = None,
         extra_meta: dict[str, Any] | None = None) -> Annotation:
    """给截图加坐标网格并压成 JPEG。

    参数
    ----
    source : ``capture.Shot`` / ``Annotation`` / ``PIL.Image``
    out_path : 文件路径或目录；``None`` 时落到系统临时目录（照样返回 path）
    step : 网格间距（标注图像素），默认 100
    max_side : 长边上限，默认 1600（``max_width`` 给了就顶掉它，方便接
        ``config["agent"]["screenshot_max_width"]``）
    quality : JPEG 质量，默认 85
    crosshair : 可选十字准星，坐标是**源图像素**（Shot 坐标），会按缩放换算
    """
    if int(step) <= 0:
        raise ValueError(f"step 必须为正，收到 {step!r}")
    limit = int(max_width if max_width is not None else max_side)
    if limit < 64:
        raise ValueError(f"max_side 太小：{limit}（至少 64）")

    image, origin, shot_scale, source_obj = _source_image(source)
    src_w, src_h = image.size
    if src_w < 4 or src_h < 4:
        raise ValueError(f"图太小，没法标注：{src_w}x{src_h}")

    factor = 1.0
    if max(src_w, src_h) > limit:
        factor = limit / float(max(src_w, src_h))
        new_size = (max(1, int(round(src_w * factor))),
                    max(1, int(round(src_h * factor))))
        image = image.resize(new_size, Image.LANCZOS)
    else:
        new_size = (src_w, src_h)

    # 标注图像素 → 物理像素
    scale = float(shot_scale) * (src_w / float(new_size[0]))
    if not scale or scale <= 0:
        scale = 1.0

    cross_in_annotated = None
    if crosshair is not None:
        cross_in_annotated = (float(crosshair[0]) * new_size[0] / src_w,
                              float(crosshair[1]) * new_size[1] / src_h)

    font_size = int(font_size) if font_size else max(11, min(16, new_size[1] // 60))
    annotated = _draw_grid(image, step=int(step), labels=bool(labels),
                           crosshair=cross_in_annotated, font_size=font_size)
    payload = encode_jpeg(annotated, quality=int(quality))
    target = _resolve_out_path(out_path)
    target.write_bytes(payload)

    meta: dict[str, Any] = {
        "source_size": [src_w, src_h],
        "resized": factor < 1.0,
        "scale": round(float(scale), 6),
    }
    if extra_meta:
        meta.update(extra_meta)

    return Annotation(
        image=annotated,
        shot=source_obj,
        path=str(target),
        jpeg=payload,
        grid_step=int(step),
        origin=(int(origin[0]), int(origin[1])),
        scale=float(scale),
        quality=int(quality),
        meta=meta,
    )


#: 别名：``annotate.grid`` 读起来更顺
annotate = grid
