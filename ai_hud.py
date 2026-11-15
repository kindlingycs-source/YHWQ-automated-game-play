# -*- coding: utf-8 -*-
"""AI 托管悬浮球 —— 鼠标悬停展开指令框，移开恢复对弈

老板 2026-11-08 定：
    「AI 操控页面的悬浮球是一个 AI 指令对话框，当鼠标悬停在这个悬浮球上时，
      会停止操控和对弈，鼠标移开悬浮球时恢复。」

实现要点
------------------------------------------------------------
1) **悬停 = 暂停**：进入球/展开面板 → 调 on_hover(True)，主程序暂停自动对弈
   和 Agent 动作；移开 → on_hover(False)，只恢复「因悬停而暂停」的状态。
2) **用轮询判断鼠标是否在里面**，不靠 <Enter>/<Leave>：
   Tk 在子控件之间移动会连续触发 Leave/Enter，直接绑会抖成一团。
   每 180ms 用 winfo_pointerxy 和自己/面板的 bbox 比一次，稳定。
3) **折叠时不能压棋盘**：位置算法和 hud.FloatBall 同口径（棋盘右侧，其次左/上/下）。
   展开时允许盖住棋盘 —— 此刻已经暂停，不会吃掉落子点击。
4) 不抢焦点、不进 Alt+Tab（WS_EX_TOOLWINDOW），和自动对弈的球同一套规矩。
5) 球上只做「说话」，不放开/关引擎按钮：所有操作都走自然语言，避免误触。
"""
from __future__ import annotations

import ctypes
import time
import tkinter as tk
from ctypes import wintypes

# 跟主界面同一套配色
BG = "#14181e"
BG2 = "#1e242c"
BG3 = "#242b34"
FG = "#e9edf2"
DIM = "#8b94a1"
ACCENT = "#d9ad72"
GREEN = "#5fbf74"
RED = "#e07070"
BLUE = "#6fa8dc"
LINE = "#2a313b"
FONT = "Microsoft YaHei UI"

BALL_W, BALL_H = 112, 112
PANEL_W, PANEL_H = 350, 430
GAP = 12


def _elide(text, max_half=30):
    """按半角宽度截断（中文算 2），避免把窗口撑大"""
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


class AIBall(tk.Toplevel):
    """AI 指令悬浮球

    on_hover(inside: bool)  进入/离开 → 主程序暂停/恢复
    on_send(text)           用户从球上发指令
    on_close()              收起球（停止托管后）
    """

    def __init__(self, master, *, on_hover=None, on_send=None, on_close=None,
                 on_open=None):
        super().__init__(master, bg=BG)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.title("野狐自动对弈·AI 悬浮球")

        self._on_hover = on_hover
        self._on_send = on_send
        self._on_close = on_close
        self._on_open = on_open
        self._expanded = False
        self._hover = False
        self._drag = None
        self._poll_job = None
        self._collapse_at = 0.0
        self._last_inside = False
        self._state = "idle"
        self._messages = []
        self._build_ball()
        self._build_panel()
        self.after(60, self._make_toolwindow)
        self._poll()

    # ------------------------------------------------------------ 构建
    def _build_ball(self):
        self.ball = tk.Frame(self, bg=BG, highlightbackground=LINE,
                             highlightthickness=1)
        self.ball.pack(fill="both", expand=True)
        cv = tk.Canvas(self.ball, width=BALL_W - 2, height=BALL_H - 2,
                       bg=BG, highlightthickness=0, bd=0)
        cv.pack()
        self._cv = cv
        cv.create_oval(4, 4, BALL_W - 6, BALL_H - 6, fill=BG2,
                       outline=LINE, width=1)
        cv.create_oval(12, 12, BALL_W - 14, BALL_H - 14, outline=BG3, width=1)
        self._dot_id = cv.create_oval(30, 24, 40, 34, fill=DIM, outline="")
        cv.create_text(BALL_W / 2 - 1, 46, text="AI", fill=FG,
                       font=(FONT, 20, "bold"))
        # 只留一行状态：112px 的球塞三行会糊在一起（2026-11-08 截图实测）
        self._state_txt = cv.create_text(
            BALL_W / 2 - 1, 76, text="待命", fill=DIM, font=(FONT, 8))
        for w in (self, self.ball, cv):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

    def _build_panel(self):
        self.panel = tk.Frame(self, bg=BG, highlightbackground=LINE,
                              highlightthickness=1)
        head = tk.Frame(self.panel, bg=BG2)
        head.pack(fill="x")
        self.p_dot = tk.Canvas(head, width=10, height=10, bg=BG2,
                               highlightthickness=0)
        self._p_dot_id = self.p_dot.create_oval(1, 1, 9, 9, fill=DIM,
                                                outline="")
        self.p_dot.pack(side="left", padx=(10, 0), pady=8)
        self.p_state = tk.Label(head, text="AI 托管 · 待命", bg=BG2, fg=FG,
                                font=(FONT, 10, "bold"), anchor="w")
        self.p_state.pack(side="left", padx=(6, 0))
        self.p_open = tk.Label(head, text="主界面", bg=BG2, fg=DIM,
                               font=(FONT, 8), cursor="hand2")
        self.p_open.pack(side="right", padx=(0, 10))
        self.p_open.bind("<Button-1>", lambda e: self._open_clicked())
        self.p_close = tk.Label(head, text="×", bg=BG2, fg=DIM,
                                font=(FONT, 12), cursor="hand2")
        self.p_close.pack(side="right", padx=(0, 8))
        self.p_close.bind("<Button-1>", lambda e: self._close_clicked())
        for w in (head, self.p_state):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        wrap = tk.Frame(self.panel, bg="#12161b")
        wrap.pack(fill="both", expand=True, padx=10, pady=(8, 4))
        self.text = tk.Text(wrap, width=1, height=1, bg="#12161b",
                            fg="#c7d0da", bd=0, highlightthickness=0,
                            font=(FONT, 10), padx=8, pady=6, wrap="word",
                            state="disabled", selectbackground="#3a4656")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("user", foreground=ACCENT,
                                font=(FONT, 10, "bold"))
        self.text.tag_configure("assistant", foreground=FG)
        self.text.tag_configure("chat", foreground=GREEN)
        self.text.tag_configure("system", foreground=DIM,
                                font=(FONT, 9))
        self.text.tag_configure("err", foreground=RED)

        row = tk.Frame(self.panel, bg=BG)
        row.pack(fill="x", padx=10, pady=(2, 6))
        self.entry = tk.Entry(row, bg=BG2, fg=FG, insertbackground=FG,
                              relief="flat", font=(FONT, 10),
                              highlightthickness=1,
                              highlightbackground=LINE,
                              highlightcolor=ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda e: self._send())
        self.btn_send = tk.Button(row, text="发送", command=self._send,
                                  bg=BG3, fg=FG, activebackground="#2f3843",
                                  activeforeground=FG, relief="flat", bd=0,
                                  highlightthickness=0, cursor="hand2",
                                  font=(FONT, 10), padx=10, pady=3)
        self.btn_send.pack(side="left", padx=(6, 0))
        tk.Label(self.panel, text="鼠标移开悬浮球 → 自动恢复对弈",
                 bg=BG, fg=DIM, font=(FONT, 8), anchor="w").pack(
            fill="x", padx=12, pady=(0, 6))

    def _make_toolwindow(self):
        try:
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)
            try:
                dw = ctypes.windll.dwmapi
                pref = ctypes.c_int(2)
                dw.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------ 悬停
    def _poll(self):
        try:
            inside = self._pointer_inside()
            if inside != self._last_inside:
                self._last_inside = inside
                if inside:
                    self._hover = True
                    self._collapse_at = 0.0
                    if not self._expanded:
                        self._expand()
                    self._fire_hover(True)
                else:
                    self._hover = False
                    self._collapse_at = time.time() + 0.7
                self._update_ball_state()
            if (not inside and self._expanded and self._collapse_at
                    and time.time() >= self._collapse_at):
                self._collapse()
                self._fire_hover(False)
        except Exception:
            pass
        try:
            self._poll_job = self.after(180, self._poll)
        except Exception:
            pass

    def _pointer_inside(self):
        try:
            x, y = self.winfo_pointerxy()
            w = PANEL_W if self._expanded else BALL_W
            h = PANEL_H if self._expanded else BALL_H
            l, t = self.winfo_x(), self.winfo_y()
            return l - 2 <= x <= l + w + 2 and t - 2 <= y <= t + h + 2
        except Exception:
            return False

    def _fire_hover(self, inside):
        if callable(self._on_hover):
            try:
                self._on_hover(bool(inside))
            except Exception:
                pass

    def _expand(self):
        if self._expanded:
            return
        self._expanded = True
        self.ball.pack_forget()
        self.panel.pack(fill="both", expand=True)
        # 展开后往左/上收，保证整块在屏内
        self._place_expanded()
        self._refresh_text()
        try:
            self.entry.focus_set()
        except Exception:
            pass

    def _collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self.panel.pack_forget()
        self.ball.pack(fill="both", expand=True)
        self._place_collapsed()
        self._update_ball_state()

    # ------------------------------------------------------------ 位置
    def _work_area(self, hwnd=None):
        try:
            u = ctypes.windll.user32

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]

            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            mon = u.MonitorFromWindow(hwnd, 2) if hwnd else 0
            if not mon:
                mon = u.MonitorFromPoint(wintypes.POINT(0, 0), 2)
            if mon and u.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                if r.right - r.left > 200 and r.bottom - r.top > 200:
                    return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
        try:
            import hud
            aw, ah, ax, ay = hud.work_area_fallback()
            return ax, ay, ax + aw, ay + ah
        except Exception:
            return 0, 0, 1920, 1040

    def _board_rect(self, hwnd, geom):
        """棋盘屏幕矩形；没有几何就退回窗口（宁可多躲）"""
        try:
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
            ww, wh = wr - wl, wb - wt
            side = min(ww, wh) * 0.88
            if side <= 0:
                return wl, wt, wr, wb
            bl = wl + max(0, (ww - side) * 0.22)
            bt = wt + (wh - side) * 0.5
            return bl, bt, bl + side, bt + side
        except Exception:
            return 0, 0, 0, 0

    def place_beside(self, hwnd=None, geom=None):
        self._hwnd = hwnd
        self._geom = geom
        if self._expanded:
            self._place_expanded()
        else:
            self._place_collapsed()

    def _place_collapsed(self):
        try:
            work = self._work_area(getattr(self, "_hwnd", None))
            bl, bt, br, bb = self._board_rect(getattr(self, "_hwnd", None),
                                              getattr(self, "_geom", None))
            x = work[2] - BALL_W - GAP
            y = bt + (bb - bt - BALL_H) // 2
            if x < br + 6:
                x = bl - BALL_W - GAP
            if x < work[0] + 4:
                x = min(max(br - BALL_W, work[0] + 4), work[2] - BALL_W - 4)
                y = bt - BALL_H - GAP
                if y < work[1] + 4:
                    y = bb + GAP
                if y + BALL_H > work[3] - 4:
                    y = work[3] - BALL_H - 4
            x = max(work[0] + 4, min(x, work[2] - BALL_W - 4))
            y = max(work[1] + 4, min(y, work[3] - BALL_H - 4))
            self.geometry(f"{BALL_W}x{BALL_H}+{int(x)}+{int(y)}")
        except Exception:
            self.geometry(f"{BALL_W}x{BALL_H}+40+40")

    def _place_expanded(self):
        try:
            work = self._work_area(getattr(self, "_hwnd", None))
            bl, bt, br, bb = self._board_rect(getattr(self, "_hwnd", None),
                                              getattr(self, "_geom", None))
            x = work[2] - PANEL_W - GAP
            y = max(work[1] + 4, min(bt + (bb - bt - PANEL_H) // 2,
                                    work[3] - PANEL_H - 4))
            if x < bl + 8 and bl - PANEL_W - GAP > work[0] + 4:
                x = bl - PANEL_W - GAP      # 右侧放不下 → 棋盘左边
            x = max(work[0] + 4, min(x, work[2] - PANEL_W - 4))
            self.geometry(f"{PANEL_W}x{PANEL_H}+{int(x)}+{int(y)}")
        except Exception:
            self.geometry(f"{PANEL_W}x{PANEL_H}+40+40")

    # ------------------------------------------------------------ 拖拽
    def _drag_start(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if not self._drag:
            return
        try:
            self.geometry(f"+{int(e.x_root - self._drag[0])}"
                          f"+{int(e.y_root - self._drag[1])}")
        except Exception:
            pass

    def _drag_end(self, _e):
        self._drag = None

    # ------------------------------------------------------------ 状态/消息
    def set_state(self, text, color=DIM, state=None):
        if state:
            self._state = state
        try:
            self._cv.itemconfigure(self._state_txt, text=_elide(text, 14),
                                   fill=color)
            self._cv.itemconfigure(self._dot_id, fill=color)
            self.p_state.configure(text="AI 托管 · " + _elide(text, 22))
            self.p_dot.itemconfigure(self._p_dot_id, fill=color)
        except Exception:
            pass

    def _update_ball_state(self):
        color = {"idle": DIM, "running": GREEN, "paused": ACCENT,
                 "thinking": BLUE, "error": RED}.get(self._state, DIM)
        try:
            self._cv.itemconfigure(self._dot_id, fill=color)
        except Exception:
            pass

    def set_messages(self, messages):
        self._messages = list(messages or [])
        if self._expanded:
            self._refresh_text()

    def add_message(self, role, text, ts=""):
        self._messages.append({"role": role, "text": str(text), "ts": ts})
        del self._messages[:-60]
        if self._expanded:
            self._append_one(self._messages[-1])

    def _refresh_text(self):
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            for m in self._messages[-30:]:
                self._append_one(m)
            self.text.see("end")
            self.text.configure(state="disabled")
        except Exception:
            pass

    def _append_one(self, m):
        try:
            role = m.get("role") or "assistant"
            tag = {"user": "user", "assistant": "assistant",
                   "chat": "chat", "system": "system",
                   "err": "err"}.get(role, "assistant")
            who = {"user": "我", "assistant": "AI", "chat": "聊天",
                   "system": "系统", "err": "错误"}.get(role, "AI")
            ts = m.get("ts") or ""
            self.text.configure(state="normal")
            self.text.insert("end", f"{who} {ts}\n", tag)
            self.text.insert("end", f"{m.get('text')}\n\n", tag)
            self.text.see("end")
            self.text.configure(state="disabled")
        except Exception:
            pass

    # ------------------------------------------------------------ 发送
    def _send(self):
        try:
            text = self.entry.get().strip()
        except Exception:
            return
        if not text:
            return
        try:
            self.entry.delete(0, "end")
        except Exception:
            pass
        if callable(self._on_send):
            try:
                self._on_send(text)
            except Exception:
                pass

    def _close_clicked(self):
        # 展开面板上的 ×：先收起，交给主程序决定是否停止托管
        self._collapse()
        self._fire_hover(False)
        if callable(self._on_close):
            try:
                self._on_close()
            except Exception:
                pass

    def _open_clicked(self):
        """回主界面（不停托管，球先收起）"""
        self._collapse()
        self._fire_hover(False)
        if callable(self._on_open):
            try:
                self._on_open()
            except Exception:
                pass

    # ------------------------------------------------------------ 显隐
    def show(self):
        try:
            self.deiconify()
            self.attributes("-topmost", True)
            self.lift()
            self._place_collapsed()
        except Exception:
            pass

    def hide(self):
        try:
            self.withdraw()
        except Exception:
            pass

    def destroy(self):
        try:
            if self._poll_job:
                self.after_cancel(self._poll_job)
        except Exception:
            pass
        super().destroy()
