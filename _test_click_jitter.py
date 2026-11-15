# -*- coding: utf-8 -*-
"""落子点击随机偏移的验收（2026-11-01）

验三件事：
1) 偏移量 ≤ 方格宽度的 10%（且 < 半格）
2) 偏移**不改变落点**：把点击坐标反算回最近的交叉点，必须还是原来那个
3) 每次点击的像素坐标不一样（不是同一个点重复点）

跑法：python _test_click_jitter.py    （不需要野狐、不点真鼠标）
"""
import random
import sys

import fox_board as FB

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  [x]", msg)
    return cond


def to_grid(sx, sy, wx, wy, g):
    """屏幕坐标 → 最近交叉点序号（野狐就是这么判的）"""
    return (round((sx - wx - g["x0"]) / g["step"]),
            round((sy - wy - g["y0"]) / g["step"]))


def test_jitter_bounds():
    print("[1] 偏移量上限")
    cases = [("19路 38.0px", 38.016), ("13路 55px", 55.0),
             ("9路 81px", 81.0), ("小窗 19路 19px", 19.0),
             ("异常 0px", 0.0)]
    for name, step in cases:
        d = [FB._click_jitter(step) for _ in range(2000)]
        m = max((dx * dx + dy * dy) ** 0.5 for dx, dy in d)
        want = min(FB.CLICK_JITTER_MAX, step * FB.CLICK_JITTER_RATIO,
                   step * FB.CLICK_JITTER_SAFE)
        print(f"  {name:<14} 最大 {m:5.2f}px  允许 {want:5.2f}px  "
              f"半格 {step/2:6.2f}px")
        check(m <= want + 1e-9, f"{name} 偏移超上限")
        check(m < step / 2 if step else True, f"{name} 偏移超过半格")
        check(want > 0 or m == 0, f"{name} 上限为 0 却有偏移")


def test_same_point():
    print("[2] 反算最近交叉点 —— 200 次点击必须都还是原来那个点")
    for name, geom, wx, wy, move in [
        ("19路", {"x0": 95.0, "y0": 78.0, "step": 38.016, "n": 19},
         261, 59, "D16"),
        ("19路·边角", {"x0": 297.5, "y0": 95.5, "step": 71.0556, "n": 19},
         -10, 0, "T19"),
        ("13路", {"x0": 120.0, "y0": 140.0, "step": 55.0, "n": 13},
         0, 30, "A1"),
        ("9路", {"x0": 60.0, "y0": 66.0, "step": 81.0, "n": 9}, 5, 7, "J9"),
    ]:
        seen = []
        FB.ensure_ready = lambda allow_restore=True: (9, wx, wy, 999, 999, "t")
        FB.activate = lambda hwnd, allow_restore=True: True
        # v3.0.2 起 play_move 在点之前会问「这个像素归谁」，被挡就不点；
        # 离线验收没有真野狐窗口，这里让落点一律算作野狐（hwnd=9）。
        FB.window_at_point = lambda sx, sy: 9
        FB.click_at = lambda sx, sy: seen.append((sx, sy))
        g = dict(geom, ok=True)
        x, y = FB.from_gtp(move, g["n"])
        ok = True
        for _ in range(200):
            FB.play_move(move, confirm=False, wait=0, geom=g)
            gx, gy = to_grid(seen[-1][0], seen[-1][1], wx, wy, g)
            if (gx, gy) != (x, y):
                ok = False
                break
        uniq = len(set(seen))
        print(f"  {name:<10} {move:<4} 200 次反算全中 = {ok}　"
              f"不同坐标 {uniq} 种")
        check(ok, f"{name} {move} 有偏移把点算到别的交叉点上去了")
        check(uniq > 190, f"{name} {move} 像素坐标重复太多（看着像机器）")


def test_seed_reproducible():
    print("[3] 固定种子可复现（测试用）")
    r1, r2 = random.Random(1), random.Random(1)
    a = [FB._click_jitter(38.0, r1) for _ in range(3)]
    b = [FB._click_jitter(38.0, r2) for _ in range(3)]
    check(a == b, "固定种子结果不一致")
    check(len(set(a)) == 3, "同一次随机里三个偏移全一样")
    print("  [ok]", [f"({x:+.2f},{y:+.2f})" for x, y in a])


def main():
    test_jitter_bounds()
    test_same_point()
    test_seed_reproducible()
    print()
    if FAIL:
        print(f"[x] 失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("[ok] 全部通过：偏移够小、落点不变、坐标每次都不同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
