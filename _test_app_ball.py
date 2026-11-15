# -*- coding: utf-8 -*-
"""整机联调：真 App + 悬浮球（不开引擎、不连野狐、不点鼠标）

验的是「接线」而不是界面像素：
  1) App 能建起来（v3.1.0 主窗口）
  2) _show_ball() 会把球摆到棋盘右边，并把状态/日志同步过去
  3) app.log() 的每一行都会喂到球的第二行
  4) _finish() 后球显示「已停止」、主键变「开启引擎」

跑法：python _test_app_ball.py
"""
import os
import sys
import tempfile
import time

# 用临时数据目录：不碰老板真实的 settings.json / logs
_TMP = tempfile.mkdtemp(prefix="weiqi-test-")
os.environ["WEIQI_HOME"] = _TMP

import tkinter as tk

import fox_board as FB
import gui
import hud


FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  [x]", msg)
    return cond


def shot(win, name):
    import ctypes
    from ctypes import wintypes
    win.update_idletasks()
    hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    im = FB._print_window(hwnd, r.right - r.left, r.bottom - r.top)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_tests", name)
    if im is not None:
        im.save(path)
        print(f"  截图 {path}  {im.size[0]}x{im.size[1]}")


def main():
    # 假的棋盘几何 + 假装找不到野狐窗口（球会按屏幕右边摆）
    FB.GEOM = {"x0": 297.5, "y0": 95.5, "step": 71.0556, "n": 19, "ok": True}
    FB.find_hwnd = lambda: []

    root = tk.Tk()
    app = gui.App(root)
    root.update()

    print("[1] 主窗口")
    check(gui.APP_VERSION == "3.1.6", "版本号没到 3.1.6")
    shot(root, "_main_v310.png")

    print("[2] 开球")
    app.running = True
    app.paused = False
    app._show_ball()
    root.update()
    check(app.ball is not None, "球没建起来")
    ball = app.ball
    app._sync_ball()          # 跟真机一样：状态字带「我执黑/白」
    ball.set_logs(["引擎就绪：智子VIP共享　规则=中国规则",
                   "→ D16　胜率 62.3%　目差 +3.5"])
    root.update()
    bx, by = ball.winfo_x(), ball.winfo_y()
    print(f"  球位置 {bx},{by} {hud.W}x{hud.H}")
    br = 297.5 + 71.0556 * 18
    check(bx >= br, "球压到棋盘右边了")
    shot(ball, "_ball_v310.png")

    print("[3] 日志同步")
    app.log("测试：思考时间 0.5 秒")
    root.update()
    check("测试：思考时间" in ball.log2.cget("text") or
          "测试：思考时间" in ball.log1.cget("text"),
          f"日志没喂到球上：{ball.log1.cget('text')!r} / {ball.log2.cget('text')!r}")

    print("[4] 暂停 → 继续")
    app.auto = None
    app.pause()
    root.update()
    check(app.paused, "pause() 没生效")
    check(ball.btn_pause.cget("text").endswith("继续"), "球上没变「继续」")
    check(ball.state_lbl.cget("text") == "已暂停", "球状态不是已暂停")
    app.resume()
    root.update()
    check(not app.paused, "resume() 没生效")
    check(ball.btn_pause.cget("text").endswith("暂停"), "球上没变回「暂停」")

    print("[5] 收工")
    app._finish()
    root.update()
    check(ball.state_lbl.cget("text") == "已停止", "收工后球不是已停止")
    check(ball.btn_engine.cget("text").endswith("开启引擎"), "收工后主键没变开启")
    check(app.btn_pause.cget("text") == "⏸  暂停", "主界面按钮还带 ESC")
    shot(ball, "_ball_stopped_v310.png")

    try:
        root.destroy()
    except Exception:
        pass

    print()
    if FAIL:
        print(f"[x] 失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("[ok] 整机联调通过：球出现/位置/日志/暂停/收工都对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
