# -*- coding: utf-8 -*-
"""落点门槛的验收（2026-11-02）

背景：v3.0.3 在 play_move 里「抢不到前台就拒绝点击」，本窗口一自动
最小化，后台进程经常抢不到前台 → 一手都不下。现在改成：
**只有「落点像素不属于野狐」才拒绝**，抬窗口降级成尽力而为。

跑法：python _test_click_gate.py   （不需要野狐、不点真鼠标）
"""
import sys

import fox_board as FB

FAIL = []
GEOM = {"x0": 95.0, "y0": 78.0, "step": 38.016, "n": 19, "ok": True}
FOX = 111


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  [x]", msg)
    return cond


def _stub(owner):
    """把窗口/点击全换成假的；owner 是「落点像素归谁」"""
    clicks = []
    FB.ensure_ready = lambda allow_restore=True: (FOX, 0, 0, 999, 999, "t")
    FB.activate = lambda hwnd, allow_restore=True: False   # 故意抢不到前台
    FB.window_at_point = lambda sx, sy: owner
    FB.force_top = lambda hwnd: None
    FB.click_at = lambda sx, sy: clicks.append((sx, sy))
    return clicks


def test_activation_failure_does_not_block():
    print("[1] 抢不到前台，但落点属于野狐 → 必须照点")
    clicks = _stub(FOX)
    ok, det = FB.play_move("D16", confirm=False, wait=0, geom=GEOM)
    print(f"  ok={ok}  点击次数={len(clicks)}  detail={det}")
    check(ok, "抢不到前台时把这一手放弃了（老 bug 复现）")
    # 2026-11-02 起抢不到前台会补点第二下（第一下可能只算激活窗口）
    check(len(clicks) in (1, 2), "没有真的点下去")
    check(len(set(clicks)) == 1, "补点的位置变了（应点同一个交叉点）")


def test_foreign_window_still_blocks():
    print("[2] 落点被别的窗口挡住 → 仍然必须拒绝（不能点歪）")
    clicks = _stub(999)
    ok, det = FB.play_move("D16", confirm=False, wait=0, geom=GEOM)
    print(f"  ok={ok}  点击次数={len(clicks)}  挡路的={det.get('blocker')}")
    check(not ok, "被挡住还点了 —— 会点到别人窗口上")
    check(len(clicks) == 0, "被挡住却发出点击")
    check(isinstance(det, dict) and det.get("blocked"), "没标记 blocked")


def main():
    test_activation_failure_does_not_block()
    test_foreign_window_still_blocks()
    print()
    if FAIL:
        print(f"[x] 失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("[ok] 全部通过：抬不动前台照点，被挡住绝不点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
