# -*- coding: utf-8 -*-
"""落点归属判据的离线验收（2026-11-02）

不需要野狐：开一个自己的小窗口当靶子，把 ensure_ready / activate /
click_at 换成假的，验三件事：

1) 落点属于目标窗口      → **直接点**，不抢焦点
2) 落点属于别的窗口      → 先抬一次，还不行就 blocked（拒绝点击）
3) blocked 的返回值带「挡路的是谁」，上层能据此只报一次

跑法：python _test_click_reach.py
"""
import ctypes
import sys
import tkinter as tk

import fox_board as FB

FAIL = []
CLICKED = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  [x]", msg)
    return cond


def main():
    root = tk.Tk()
    root.title("靶子窗口")
    root.geometry("320x220+1900+820")
    root.update()
    top = ctypes.windll.user32.GetAncestor(int(root.winfo_id()), 2)
    root.update()
    sx, sy = 1900 + 160, 820 + 110          # 窗口中央（客户区坐标近似）
    print("靶子窗口 hwnd:", top, " 测试点:", (sx, sy))

    owner = FB.window_at_point(sx, sy)
    print("WindowFromPoint ->", owner, FB.window_desc(owner))
    check(owner == top, "window_at_point 没认出靶子窗口")

    # 让 K10（x=9,y=9）正好落在测试点上：x0/y0 是**窗口内**坐标
    wx, wy = 1900, 820
    step = 40.0
    geom = {"ok": True,
            "x0": (sx - wx) - step * 9,
            "y0": (sy - wy) - step * 9,
            "step": step, "n": 19, "nlines": 19, "method": "fake"}

    # ---- 2) 落点属于别人 → blocked ----
    print("[2] 目标窗口另有其人（模拟被别的窗口挡住）")
    FB.ensure_ready = lambda *a, **k: (top + 12345, 1900, 820, 320, 220, "t")
    FB.activate = lambda *a, **k: True
    FB.force_top = lambda *a, **k: None
    FB.click_at = lambda x, y: CLICKED.append((x, y))
    ok, det = FB.play_move("K10", confirm=False, wait=0, geom=dict(geom))
    print("   ok=", ok, " blocked=", det.get("blocked"),
          " blocker=", det.get("blocker"))
    check(not ok, "被挡住时居然返回成功")
    check(bool(det.get("blocked")), "被挡住时没带 blocked 标记")
    check("靶子窗口" in str(det.get("blocker")), "挡路的窗口名没报对")
    check(not CLICKED, "被挡住时还是点了（会点到别人身上）")

    # ---- 1) 落点属于目标 → 直接点 ----
    print("[1] 目标就是靶子窗口（模拟正常情况）")
    CLICKED.clear()
    FB.ensure_ready = lambda *a, **k: (top, 1900, 820, 320, 220, "t")
    ok, det = FB.play_move("K10", confirm=False, wait=0, geom=dict(geom))
    print("   ok=", ok, " 点了:", CLICKED)
    check(ok, "正常情况反而没点出去")
    check(len(CLICKED) == 1, "正常情况点击次数不对")

    root.destroy()
    print()
    if FAIL:
        print("失败", len(FAIL), "项：", FAIL)
        return 1
    print("全部通过：归属判据可用，被挡住时不会误点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
