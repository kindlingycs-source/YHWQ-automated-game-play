# -*- coding: utf-8 -*-
"""
读取 Lizzie 当前棋局
============================================================
数据源：Lizzie 的自动存档 save/autoGame2.sgf
  · 每次落子/分析刷新都会重写这个文件（实测 03:43:47 落盘）
  · 里面同时有：盘面(SZ/AB/AW)、逐手棋谱(B/W)、Lizzie 自己的分析(LZOP)
  · LZOP 是一个字符串，内嵌 "move L13 visits 261 winrate 0 ... pv ..." 段

为什么不用 GTP 直连：
  老板的 Lizzie 引擎进程对外不可见（管道在 java 内部），
  而自动存档是它主动写盘的，读文件反而更稳，也不会干扰他下棋。
"""
import os, re, json, time


LIZZIE_DIRS = [
    # 智子客户端内嵌的那一份（老板实际在用的）
    r"C:\Users\Administrator\AppData\Local\ikatago_gui_installer\app-1.3.0\resources\data\lizzieyzy",
    # 整合包独立版
    r"D:\WEIXIN\xwechat_files\wxid_1fppd180iksb22_6818\msg\file\2026-09"
    r"\2026-09-03-windows64.without.engine.portable\LizzieYzy Next",
]

COLS = "ABCDEFGHJKLMNOPQRST"


def find_autosave():
    """找最新被写过的 autoGame*.sgf"""
    best = None
    for d in LIZZIE_DIRS:
        sv = os.path.join(d, "save")
        if not os.path.isdir(sv):
            continue
        for fn in os.listdir(sv):
            if fn.lower().startswith("autogame") and fn.lower().endswith(".sgf"):
                p = os.path.join(sv, fn)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if best is None or m > best[0]:
                    best = (m, p, d)
    return best


def parse_sgf(path):
    """解析 Lizzie 的 sgf（单行、无分支、LZOP 内嵌分析）"""
    raw = open(path, encoding="utf-8", errors="replace").read()

    def prop(name):
        m = re.search(rf"{name}\[([^\]]*)\]", raw)
        return m.group(1) if m else None

    sz = prop("SZ") or "19"
    if ":" in sz:
        w, h = sz.split(":")
        w, h = int(w), int(h)
    else:
        w = h = int(sz)

    km = prop("KM")
    komi = float(km) if km else None

    # 预置棋子：AB[..][..] / AW[..][..]
    ab = re.search(r"AB((?:\[[a-z]{2}\])+)", raw)
    aw = re.search(r"AW((?:\[[a-z]{2}\])+)", raw)

    def coords(block):
        if not block:
            return []
        out = []
        for s in re.findall(r"\[([a-z]{2})\]", block.group(1)):
            x = ord(s[0]) - 97
            y = ord(s[1]) - 97
            out.append((x, y))
        return out

    preset_b = coords(ab)
    preset_w = coords(aw)

    # 手顺：按出现顺序抓 ;B[..] / ;W[..]
    moves = []
    for m in re.finditer(r";\s*([BW])\[([a-z]{0,2})\]", raw):
        c, mv = m.group(1), m.group(2)
        if mv == "":
            moves.append({"color": c, "gtp": "pass", "xy": None})
        elif len(mv) == 2:
            x = ord(mv[0]) - 97
            y = ord(mv[1]) - 97
            moves.append({"color": c, "gtp": f"{COLS[x]}{h - y}", "xy": (x, y)})
        else:
            moves.append({"color": c, "gtp": "??", "xy": None})

    # LZOP：Lizzie 自己的分析
    lzop = prop("LZOP") or ""
    cands = []
    for seg in re.split(r"\s+(?=move\s)", lzop):
        seg = seg.strip()
        if not seg.startswith("move"):
            continue
        d = {}
        for k in ("move", "visits", "winrate", "prior", "scoreMean"):
            m = re.search(rf"\b{k}\s+(-?[\d.]+|[A-Za-z]\d+|pass)", seg)
            if m:
                v = m.group(1)
                try:
                    d[k] = float(v) if "." in v or k != "move" else v
                except ValueError:
                    d[k] = v
        pm = re.search(r"\bpv((?:\s+(?:[A-Za-z]\d+|pass))+)", seg)
        if pm:
            d["pv"] = pm.group(1).split()
        if d.get("move"):
            cands.append(d)

    # 盘面
    board = [[0] * w for _ in range(h)]
    for x, y in preset_b:
        if 0 <= x < w and 0 <= y < h:
            board[y][x] = 1
    for x, y in preset_w:
        if 0 <= x < w and 0 <= y < h:
            board[y][x] = 2
    for mv in moves:
        if mv["xy"]:
            x, y = mv["xy"]
            if 0 <= x < w and 0 <= y < h:
                board[y][x] = 1 if mv["color"] == "B" else 2

    return {
        "path": path,
        "mtime": os.path.getmtime(path),
        "w": w, "h": h, "komi": komi,
        "comment": (prop("C") or "").replace("\\n", "\n"),
        "moves": moves,
        "board": board,
        "cands": cands,
        "raw_lzop": lzop[:4000],
    }


def show(g):
    """ASCII 打印盘面 + 手顺"""
    w, h, board = g["w"], g["h"], g["board"]
    print(f"  盘面 {w}x{h}  komi={g['komi']}  手数={len(g['moves'])}")
    print("     " + " ".join(COLS[:w]))
    for y in range(h):
        row = []
        for x in range(w):
            row.append("X" if board[y][x] == 1 else ("O" if board[y][x] == 2 else "."))
        print(f"  {h-y:2d} " + " ".join(row))
    if g["moves"]:
        seq = " ".join(f"{m['color']}{m['gtp']}" for m in g["moves"][-12:])
        print(f"  最近手顺: ...{seq}")


def main():
    hit = find_autosave()
    if not hit:
        print("没找到 Lizzie 自动存档")
        return
    mtime, path, d = hit
    age = time.time() - mtime
    print(f"存档: {path}")
    print(f"来源: {d}")
    print(f"更新: {time.strftime('%H:%M:%S', time.localtime(mtime))}  （{age:.0f} 秒前）")
    print()
    g = parse_sgf(path)
    show(g)
    print()
    print(f"=== Lizzie 分析 {len(g['cands'])} 个候选点 ===")
    for c in g["cands"][:8]:
        pv = " ".join(c.get("pv", [])[:6])
        wr = c.get("winrate")
        v = c.get("visits")
        print(f"  {str(c.get('move')):>5}  胜率={wr}  visits={v}  pv={pv}")
    print()
    print("=== 注释（Lizzie 自己写的） ===")
    print(g["comment"][:400])


if __name__ == "__main__":
    main()
