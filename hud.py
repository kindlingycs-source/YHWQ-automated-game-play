# -*- coding: utf-8 -*-
"""悬浮球 —— 对局时挂在棋盘右边的小遥控器

老板 2026-11-02 定稿：
    「把 ESC 这个快捷键取消掉，改为开始对局之后，窗口最小化并在屏幕上
      出现一个悬浮球（放在右边好了，出现在棋盘之外不遮挡）。这个悬浮球中，
      有开启/关闭引擎，继续/暂停这两个按钮，还出现两行左右的日志，
      让用户大致看到引擎的启动与思考的情况。」

所以对局期间主窗口收起来，所有操作都在这颗球上：

    ┌──────────────────────────────┐
    │ ● 运行中                   × │   ← 状态点（绿/琥珀/红）+ 状态字
    │ [ ■ 关闭引擎 ] [ ⏸ 暂停 ]    │   ← 两个按钮（文字随状态变）
    │ 引擎就绪：智子VIP共享 …       │   ← 日志第 1 行
    │ → D16　胜率 62.3%　目差 +3.5 │   ← 日志第 2 行
    └──────────────────────────────┘

几条硬规矩（都是这个项目踩过的坑）：
  1) **绝对不许压在棋盘上** —— 落子点的是屏幕绝对坐标，球压住棋盘
     就等于把点击吃掉了，表现是「一手都不下」。位置从野狐窗口 +
     棋盘几何算，优先贴屏幕最右边，其次左边/上边/下边，最后才夹回屏内。
  2) **不进 Alt+Tab、不抢任务栏**（WS_EX_TOOLWINDOW + overrideredirect），
     它只是棋盘边上的一个挂件，不是第二个程序窗口。
  3) **不抢焦点**：`-topmost` 常驻，引擎抬野狐时球仍然浮在最上面，
     老板随时看得到状态、点得到按钮。
  4) 球上的字一律等宽截断（中文按 2 个半角算），不然 Tk 会把窗口撑大，
     窗口一宽就会盖到棋盘上。
"""
import ctypes
import tkinter as tk
from ctypes import wintypes

# ---------------------------------------------------------------- 配色
# 跟主界面一套色（gui.py 里的 BG/FG/ACCENT…），别在两处各调一个色。
BG = "#14181e"
BG2 = "#1e242c"
BG3 = "#242b34"
FG = "#e9edf2"
DIM = "#8b94a1"
ACCENT = "#d9ad72"
GREEN = "#5fbf74"
RED = "#e07070"
LINE = "#2a313b"

FONT = "Microsoft YaHei UI"

W, H = 320, 138          # 球的外框尺寸（物理像素，本程序是 DPI aware）
GAP = 12                 # 离屏幕边 / 窗口边的空隙


def _elide(text, max_half=34):
    """按「半角宽度」截断 —— 一个汉字算 2，英文数字算 1

    不按字符数截：中英混排时同样 20 个字符能差一倍宽，
    球会被撑大（然后盖到棋盘上）。
    """
    s = str(text or "").replace("\n", " ")
    out, w = [], 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2E7F else 1
        if w + cw > max_half:
            out.append("…")
            break
        out.append(ch)
        w += cw
    return "".join(out)


class FloatBall(tk.Toplevel):
    """对局中替 ESC 干活的那颗球

    on_engine  点「关闭引擎 / 开启引擎」
    on_pause   点「暂停 / 继续」
    on_dismiss 点右上角 ×（只有停手之后才出现）
    """

    def __init__(self, master, on_engine=None, on_pause=None,
                 on_dismiss=None):
        super().__init__(master, bg=BG)
        self.withdraw()                       # 先藏起来，别在屏幕角上闪一下
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.title("野狐自动对弈·悬浮球")

        self._on_engine = on_engine
        self._on_pause = on_pause
        self._on_dismiss = on_dismiss
        self._drag = None
        self._running = False
        self._paused = False
        self._build()
        # 50ms 后补一个「工具窗口」样式：不进 Alt+Tab，也不占任务栏。
        # 必须在窗口真正建出来之后设，太早拿不到顶层句柄。
        self.after(60, self._make_toolwindow)

    # ------------------------------------------------------------ 画
    def _build(self):
        outer = tk.Frame(self, bg=BG, highlightbackground=LINE,
                         highlightthickness=1)
        outer.pack(fill="both", expand=True)

        # ---- 第一行：状态点 + 状态字 +（停手后）关闭 × ----
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", padx=10, pady=(6, 1))
        self.dot = tk.Canvas(head, width=10, height=10, bg=BG,
                             highlightthickness=0)
        self._dot_id = self.dot.create_oval(1, 1, 9, 9, fill=DIM, outline="")
        self.dot.pack(side="left", pady=(2, 0))
        self.state_lbl = tk.Label(head, text="启动中", bg=BG, fg=FG,
                                  font=(FONT, 10, "bold"), anchor="w")
        self.state_lbl.pack(side="left", padx=(6, 0))
        self.dismiss_lbl = tk.Label(head, text="×", bg=BG, fg=DIM,
                                    font=(FONT, 12), cursor="hand2")
        self.dismiss_lbl.bind("<Button-1>", lambda e: self._dismiss())

        # ---- 第二行：两个按钮 ----
        row = tk.Frame(outer, bg=BG)
        row.pack(fill="x", padx=8, pady=(2, 2))
        self.btn_engine = tk.Button(
            row, text="■  关闭引擎", command=self._engine_clicked,
            bg=BG3, fg=FG, activebackground="#2f3843", activeforeground=FG,
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            font=(FONT, 9), padx=4, pady=3)
        self.btn_engine.pack(side="left", expand=True, fill="x")
        self.btn_pause = tk.Button(
            row, text="⏸  暂停", command=self._pause_clicked,
            bg=BG3, fg=FG, activebackground="#2f3843", activeforeground=FG,
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            font=(FONT, 9), padx=4, pady=3)
        self.btn_pause.pack(side="left", expand=True, fill="x", padx=(6, 0))

        # ---- 第三、四行：最近两条日志 ----
        self.log1 = tk.Label(outer, text="准备中…", bg=BG, fg=DIM,
                             font=(FONT, 8), anchor="w", justify="left")
        self.log1.pack(fill="x", padx=10, pady=(0, 0))
        self.log2 = tk.Label(outer, text="", bg=BG, fg=DIM,
                             font=(FONT, 8), anchor="w", justify="left")
        self.log2.pack(fill="x", padx=10, pady=(0, 1))

        # 拖拽：按住空白处能挪走（有人喜欢放左上角）
        for w in (outer, head, self.state_lbl, self.log1, self.log2):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _make_toolwindow(self):
        try:
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)
            # 圆角/阴影交给系统（Win11）；失败也无所谓
            try:
                dw = ctypes.windll.dwmapi
                pref = ctypes.c_int(2)      # DWMWCP_ROUND
                dw.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------ 位置
    def place_beside(self, hwnd=None, geom=None):
        """把自己摆到「野狐棋盘之外、屏幕右边」

        优先贴当前显示器工作区的**最右边**（老板要右边）；
        右边如果会压到棋盘（小屏），依次退到棋盘左边、上边、下边，
        全都不行才夹回屏内 —— 但那种情况下会尽量贴边，把遮挡降到最小。
        """
        try:
            work = self._work_area(hwnd)
            bl, bt, br, bb = self._board_rect(hwnd, geom)
            x = work[2] - W - GAP                    # 工作区最右边
            y = bt + (bb - bt - H) // 2              # 跟棋盘垂直居中对齐
            if x < br + 6:                           # 右边放不下 → 棋盘左边
                x = bl - W - GAP
            if x < work[0] + 4:                      # 左边也放不下 → 棋盘上方
                x = min(max(br - W, work[0] + 4), work[2] - W - 4)
                y = bt - H - GAP
                if y < work[1] + 4:                  # 上方不够 → 棋盘下方
                    y = bb + GAP
                if y + H > work[3] - 4:
                    y = work[3] - H - 4
            x = max(work[0] + 4, min(x, work[2] - W - 4))
            y = max(work[1] + 4, min(y, work[3] - H - 4))
            self.geometry(f"{W}x{H}+{int(x)}+{int(y)}")
            self.update_idletasks()
            return int(x), int(y)
        except Exception:
            # 算不出来就贴主屏右上角，至少不会压棋盘中央
            self.geometry(f"{W}x{H}+40+40")
            return 40, 40

    def _work_area(self, hwnd=None):
        """包含 hwnd 的那块显示器的工作区 (l, t, r, b)

        主界面用的是 SystemParametersInfo（只有主屏），这里要跟着
        野狐窗口走 —— 老板可能把野狐放副屏。
        """
        u = ctypes.windll.user32

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = u.MonitorFromWindow(hwnd, 2) if hwnd else 0   # NEAREST
        if not mon:
            mon = u.MonitorFromPoint(wintypes.POINT(0, 0), 2)
        if mon and u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            r = mi.rcWork
            if r.right - r.left > 200 and r.bottom - r.top > 200:
                return r.left, r.top, r.right, r.bottom
        aw, ah, ax, ay = work_area_fallback()
        return ax, ay, ax + aw, ay + ah

    def _board_rect(self, hwnd, geom):
        """棋盘在屏幕上的矩形 (left, top, right, bottom)

        有标定几何就用几何：格心 x0/y0 + 间距 step × 路数，
        再往外放 0.7 格，把棋盘的木边也算进去。
        没有几何就退回整个野狐窗口 —— 宁可多躲一点，也不能压到棋盘。
        """
        u = ctypes.windll.user32
        r = wintypes.RECT()
        if hwnd and u.GetWindowRect(hwnd, ctypes.byref(r)):
            wl, wt, wr, wb = r.left, r.top, r.right, r.bottom
        else:
            wl = wt = 0
            wr = u.GetSystemMetrics(0)
            wb = u.GetSystemMetrics(1)
        if geom and geom.get("ok") and geom.get("step"):
            step = float(geom["step"])
            n = int(geom.get("n") or 19)
            x0 = wl + float(geom["x0"])
            y0 = wt + float(geom["y0"])
            pad = step * 0.7
            return (x0 - pad, y0 - pad,
                    x0 + step * (n - 1) + pad,
                    y0 + step * (n - 1) + pad)
        # 没标定过：按「棋盘是窗口里一块接近正方形的区域」估一个。
        # 直接拿整个窗口当棋盘会让球被挤到屏幕下边缘（正好压在棋盘上），
        # 所以这里宁可估一个保守的方框：高 88%、左侧留 22% 边。
        ww, wh = wr - wl, wb - wt
        side = min(ww, wh) * 0.88
        if side <= 0:
            return wl, wt, wr, wb
        bl = wl + max(0, (ww - side) * 0.22)
        bt = wt + (wh - side) * 0.5
        return bl, bt, bl + side, bt + side

    # ------------------------------------------------------------ 拖拽
    def _drag_start(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if not self._drag:
            return
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self.geometry(f"+{int(x)}+{int(y)}")

    # ------------------------------------------------------------ 状态
    def set_state(self, text, color=GREEN):
        try:
            self.state_lbl.configure(text=text)
            self.dot.itemconfigure(self._dot_id, fill=color)
        except Exception:
            pass

    def set_logs(self, lines):
        """传最近两行（老的在前）。第二行没有就留空。"""
        lines = list(lines or [])
        a = lines[-2] if len(lines) >= 2 else (lines[-1] if lines else "")
        b = lines[-1] if len(lines) >= 2 else ""
        try:
            self.log1.configure(text=_elide(a))
            self.log2.configure(text=_elide(b))
        except Exception:
            pass

    def set_running(self, running, paused=False):
        """同步两个按钮的文字与可用状态"""
        self._running = bool(running)
        self._paused = bool(paused)
        try:
            self.btn_engine.configure(
                text="■  关闭引擎" if running else "▶  开启引擎")
            self.btn_pause.configure(
                state=("normal" if running else "disabled"),
                text="▶  继续" if paused else "⏸  暂停",
                fg=(ACCENT if paused else FG))
            if running:
                self.dismiss_lbl.pack_forget()
            else:
                self.dismiss_lbl.pack(side="right", padx=(4, 0))
        except Exception:
            pass

    # ------------------------------------------------------------ 点击
    def _engine_clicked(self):
        if callable(self._on_engine):
            self._on_engine()

    def _pause_clicked(self):
        if callable(self._on_pause):
            self._on_pause()

    def _dismiss(self):
        if callable(self._on_dismiss):
            self._on_dismiss()
        self.withdraw()

    # ------------------------------------------------------------ 显隐
    def show(self):
        try:
            self.deiconify()
            self.attributes("-topmost", True)
            self.lift()
        except Exception:
            pass

    def hide(self):
        try:
            self.withdraw()
        except Exception:
            pass


def work_area_fallback():
    """拿不到显示器信息时的兜底：主屏可用区（跟 gui.work_area 同口径）"""
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(r), 0):
            return (r.right - r.left, r.bottom - r.top, r.left, r.top)
    except Exception:
        pass
    return 1920, 1040, 0, 0
