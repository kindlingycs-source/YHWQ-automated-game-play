# -*- coding: utf-8 -*-
"""悬浮球的离线验收：长得对不对、按钮通不通、会不会压棋盘

跑法：python _test_ball.py
产物：_tests/ball_running.png / ball_paused.png / ball_stopped.png

不开引擎、不连野狐、不点真鼠标；只把球画出来拍三张图，
再模拟点两下按钮，确认信号能接上。
"""
import ctypes
import os
import sys
import time

import tkinter as tk

import fox_board as FB
import hud


FAIL = []
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_tests")


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  [x]", msg)
    return cond


def shot(ball, name):
    os.makedirs(OUT, exist_ok=True)
    ctypes.windll.user32.UpdateWindow(
        ctypes.windll.user32.GetAncestor(ball.winfo_id(), 2))
    hwnd = ctypes.windll.user32.GetAncestor(ball.winfo_id(), 2)
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    im = FB._print_window(hwnd, r.right - r.left, r.bottom - r.top)
    path = os.path.join(OUT, name)
    if im is None:
        print("  !! PrintWindow 失败")
        return None
    im.save(path)
    print(f"  截图 {path}  {im.size[0]}x{im.size[1]}")
    return im


def main():
    hits = []
    root = tk.Tk()
    root.withdraw()
    root.title("ball-test")
    ball = hud.FloatBall(root,
                         on_engine=lambda: hits.append("engine"),
                         on_pause=lambda: hits.append("pause"))

    # ---- 运行中：两个按钮都在，× 不该出现 ----
    print("[1] 运行中")
    ball.set_state("运行中", "#5fbf74")
    ball.set_logs(["引擎就绪：智子VIP共享　规则=中国规则",
                   "→ D16　胜率 62.3%　目差 +3.5"])
    ball.set_running(True, False)
    ball.place_beside(None, None)
    ball.show()
    root.update()
    time.sleep(0.35)
    root.update()
    shot(ball, "ball_running.png")
    check(ball.btn_engine.cget("text").endswith("关闭引擎"), "运行中按钮文字不对")
    check(ball.btn_pause.cget("text").endswith("暂停"), "暂停按钮文字不对")
    check(ball.log2.cget("text").startswith("→ D16"), "第二行日志没显示")

    # ---- 暂停：球还在，按钮变「继续」，状态点琥珀 ----
    print("[2] 暂停")
    ball.set_state("已暂停", "#d9ad72")
    ball.set_running(True, True)
    root.update()
    time.sleep(0.2)
    root.update()
    shot(ball, "ball_paused.png")
    check(ball.btn_pause.cget("text").endswith("继续"), "暂停后按钮没变「继续」")

    # ---- 停手：出现 ×，主键变「开启引擎」 ----
    print("[3] 已停止")
    ball.set_state("已停止", "#8b94a1")
    ball.set_logs(["— 已结束 —", ""])
    ball.set_running(False, False)
    root.update()
    time.sleep(0.2)
    root.update()
    shot(ball, "ball_stopped.png")
    check(ball.btn_engine.cget("text").endswith("开启引擎"), "停止后没变「开启引擎」")
    check(ball.dismiss_lbl.winfo_ismapped(), "停止后没出现 ×")

    # ---- 按钮点击能不能通（暂停键停手后是灰的，先切回运行态）----
    print("[4] 按钮回调")
    ball.set_running(True, False)
    root.update()
    ball.btn_engine.invoke()
    ball.btn_pause.invoke()
    root.update()
    print("  收到:", hits)
    check(hits == ["engine", "pause"], "按钮回调没接上")

    # ---- 位置：不压棋盘 ----
    print("[5] 位置")
    geom = {"x0": 297.5, "y0": 95.5, "step": 71.0556, "n": 19, "ok": True}
    wx, wy = 0, 0
    ball.place_beside(None, geom)
    x, y = ball.winfo_x(), ball.winfo_y()
    bl = wx + geom["x0"] - geom["step"]
    br = wx + geom["x0"] + geom["step"] * 18
    print(f"  球 {x},{y} {hud.W}x{hud.H}　棋盘横范围 {bl:.0f}..{br:.0f}")
    check(x >= br or x + hud.W <= bl, "球压到棋盘横范围了（会吃掉点击）")

    ball.destroy()
    root.destroy()
    print()

    if FAIL:
        print(f"[x] 失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("[ok] 悬浮球：三态正确、按钮通、位置在棋盘外")
    return 0


if __name__ == "__main__":
    sys.exit(main())
