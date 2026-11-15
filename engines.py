# -*- coding: utf-8 -*-
"""
引擎注册表（老板钦定 16 项）
============================================================
只保留老板 2026-10-22 指定的序号，跟 Lizzie「选择要加载的引擎」对话框
里的编号一一对应：

    1~8    智子云端（VIP fdx / 1x / 3x / 6x / 12x / 24x / 让2子 / 让4子）
    10     智子VIP 18b（小棋盘通用）
    18~23  本机 KataGo（11B / 40B / 28B / 18B / 60B / 20B）
    30     18b 死活对局 Kill-all

被剔掉的：9/17/29/40（分割线）、11~16（智子其它权重，老板不要）、
24~28（怀旧 g170 系列）、31~39（大棋盘/矩形/人谱/怀旧 LZ）、
41~44（说明文字）、45~51（重复条目）。

fix：本机权重路径取 `LizzieYzy Next`
------------------------------------------------------------
老板 2026-10-22 指出的：「不用下载，本机本来就有」。
先前我在 智子客户端自带的 lizzieyzy 目录 里找，只有旧的四份；
真正在用的是微信传过来的 LizzieYzy Next，**B11 就在里面**
（kata1-tf3-b11c768-s12000M-d6278M.bin.gz，189.6MB）。
所以这里直接读 Lizzie 的 config.txt，命令原样拿来，不再自己拼路径。
"""
import os, re, json

import runtime_paths as RP

# 打包后 HERE 指向 _MEIPASS（只读资源），不要往这里写东西
HERE = RP.HERE

# 老板要的序号
WANTED = (1, 2, 3, 4, 5, 6, 7, 8, 10, 18, 19, 20, 21, 22, 23, 30)

# 智子客户端配置（引擎列表的唯一真相来源）
def _lizzie_cfg_candidates():
    """Lizzie config.txt 可能在的地方（全部**按当前用户**推，不写死用户名）

    ⚠️ 2026-10-23 修的分发级 bug
    ----------------------------------------------------
    原来这里是两个写死的绝对路径：
        D:\\WEIXIN\\...\\LizzieYzy Next\\config.txt          （老板自己的微信目录）
        C:\\Users\\Administrator\\AppData\\...\\config.txt   （我自己的用户名）
    别人的电脑上这两条**必然都不存在**，但当时这个 bug 是**隐形**的 ——
    因为读不到 config.txt 就走 `FALLBACK`（1 个云端项），程序不崩，
    界面看起来"正常"，只是引擎列表莫名其妙只剩 1 项。
    第一次真正暴露是在"模拟新电脑"测试里：
    「已登记引擎 16 项」——在一个空用户目录里怎么会有 16 项？
    因为 _find 用的还是**本机真实路径**，读到了我自己那份 config.txt。
    也就是说：**这个"新电脑测试"本身是假的**。

    现在改成：
        ① 环境变量 %LOCALAPPDATA% 推智子客户端目录（每台机器都对）
        ② 版本号目录扫一遍（app-1.3.0 / app-1.4.2 …）
        ③ 老板的微信目录保留为**最后**候选 —— 他自己机器上还能用，
           别人机器上 os.path.isfile 直接跳过，零副作用
        ④ 再加「exe 同目录/lizzieyzy」这种绿色便携位置
    """
    out = []
    la = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")

    # ⚠️ 顺序很重要（2026-10-23）：老板的引擎列表**必须不变**
    # ----------------------------------------------------
    # 老板原话「我已经设置好了的引擎不变」。原来第一个候选是微信
    # 传的 LizzieYzy Next（有 B11/40B 那 16 项），第二个才是智子
    # 客户端自带的（只有 4 份老权重）。
    # 如果我把「智子客户端（动态路径）」提到前面，老板机器上会**先命中**
    # 客户端那份 → 引擎列表从 16 项变成 4 项 → 正好违反他的要求。
    # 所以：绿色便携位置放最前（新用户放一份就在这），
    #       然后**保持**微信 → 智子客户端 的原顺序。

    # ① 绿色便携：exe 旁边放 lizzieyzy/config.txt（发给别人时最省事）
    #    ⚠️ 源码态下 RP.USER_DIR 和 RP.HERE 是同一个目录，会出两条相同的
    #    候选 —— 无害但没必要，用 set 去重一下（2026-10-23）
    for p in (os.path.join(RP.USER_DIR, "lizzieyzy", "config.txt"),
              os.path.join(RP.HERE, "lizzieyzy", "config.txt"),
              os.path.join(RP.USER_DIR, "config.txt")):
        if p not in out:
            out.append(p)

    # ② 老板本机的微信目录（别人的机器上 isfile = False，自动跳过）
    #
    # ⚠️ 实测（2026-10-23）：这个目录里**没有** config.txt，
    #    真正的配置在它下面的 `app/` 里。原来那条路径从来没命中过，
    #    16 项一直是智子客户端那份给的。两个都列上，
    #    这样老板以后换成 Lizzie 本体时还能自动认出来。
    #
    # ⚠️ 为什么还要 WEIQI_NOHOME 这个开关
    # ----------------------------------------------------
    # 做「新电脑测试」时，只改 USERPROFILE / LOCALAPPDATA 是不够的 ——
    # `D:\WEIXIN\...` 是**绝对路径**，跟用户目录无关，仍然会被读到。
    # 加了这个开关，测试就能真的模拟"这台机器上没有那份配置"。
    if not os.environ.get("WEIQI_NOHOME"):
        _wx = (r"D:\WEIXIN\xwechat_files\wxid_1fppd180iksb22_6818\msg\file"
               r"\2026-09\2026-09-03-windows64.without.engine.portable"
               r"\LizzieYzy Next")
        out.append(os.path.join(_wx, "config.txt"))
        out.append(os.path.join(_wx, "app", "config.txt"))

    # ③ 智子客户端自带（版本目录扫描，别写死 app-1.3.0）
    base = os.path.join(la, "ikatago_gui_installer")
    try:
        names = sorted(os.listdir(base), reverse=True)
    except Exception:
        names = []
    for name in names:
        if not name.lower().startswith("app-"):
            continue
        for sub in ("lizzieyzy", ""):
            out.append(os.path.join(base, name, "resources", "data", sub,
                                    "config.txt"))

    # ④ 通用安装位置
    out.append(os.path.join(la, "Programs", "LizzieYzy", "config.txt"))
    out.append(os.path.join(os.path.expanduser("~"), "LizzieYzy", "config.txt"))
    return out


LIZZIE_CFG_CANDIDATES = _lizzie_cfg_candidates()

# 智子云引擎计费说明
#
# ⚠️ vip-share 别写「免费」（2026-10-23 老板纠正）
# ----------------------------------------------------
# 原本文案是「VIP 免费·不限时」，老板看到后说：
#     「把vip免费不限时这行字去掉，vip也是要充钱的嘛」
# 他说得对。vip-share 对**调用**确实不计时不计费，但前提是你先充了
# VIP —— 把它标成「免费」会让人以为注册就能白嫖，实际用起来发现要钱。
# 界面上的字要经得起推敲，所以改成「VIP 不限时」（说清事实：不限时），
# 不再暗示免费。
_CLOUD_BILLING = {
    "vip-share": "VIP 不限时",
    "share": "免费·每天1小时·5分钟断",
    "1x": "约 0.8~1.5 元/小时",
    "3x": "2.4 元/小时",
    "6x": "4.8 元/小时",
    "12x": "9.6 元/小时",
    "24x": "19.2 元/小时",
}

# 权重强度参考（等访问量下的 kata1 官方 rating 量级，仅作提示）
WEIGHT_STRENGTH = {
    "fdx": 14595, "11b": 14595, "11b768t": 14595,
    "40b": 14500, "28bnbt": 14103, "b28": 14103,
    "18bnbt": 13800, "18b": 13800, "20b": 13400,
    "60b": 14600,
}


# ---------------------------------------------------------------- 配置解析

def cfg_path():
    for p in LIZZIE_CFG_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _blocks():
    """从 config.txt 切出 engine-settings-list 的每个条目（花括号配平）

    坑：早先用惰性正则跨 block 匹配 command 与 name，碰上长 block
    会跨条目配对，出现「名字是 A、命令是 B」的错配。
    按 `{ ... }` 切块，每块内独立取字段，不会串。
    """
    p = cfg_path()
    if not p:
        return []
    try:
        s = open(p, encoding="utf-8", errors="ignore").read()
    except Exception:
        return []
    m = re.search(r'"engine-settings-list"\s*:\s*\[', s)
    if not m:
        return []
    tail = s[m.end() - 1:]
    blocks, depth, cur = [], 0, []
    for ch in tail:
        if ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth -= 1
            cur.append(ch)
            if depth == 0:
                blocks.append("".join(cur))
                cur = []
        elif depth > 0:
            cur.append(ch)
        elif ch == "]" and depth == 0:
            break
    out = []
    for i, b in enumerate(blocks, 1):
        if i not in WANTED:
            continue
        d = None
        try:
            d = json.loads(b)
        except Exception:
            try:
                d = json.loads(b.replace("\\\\", "/"))
            except Exception:
                continue
        d["_idx"] = i
        out.append(d)
    return out


def _is_cloud(cmd):
    return cmd.strip().startswith("./zz-ikatago")


def _gpu_of(cmd):
    m = re.search(r"--gpu-type\s+(\S+)", cmd)
    return m.group(1) if m else "vip-share"


def _weight_of(cmd, name="", gpu="vip-share"):
    m = re.search(r"--kata-weight\s+(\S+)", cmd)
    if m:
        return m.group(1)
    mt = re.search(r"(\d+b)\s*天梯", name or "")
    if mt:
        return {"40b": "40b", "28b": "28bnbt", "20b": "20b",
                "18b": "18bnbt", "11b": "11b768t",
                "10b": ("10b512t" if "中" in (name or "") else "10b384t")
                }.get(mt.group(1), "fdx")
    return "20b" if gpu == "share" else "fdx"


def _extra_of(cmd):
    """透传参数：--kata-config（让子局）、--kata-name（换后端）"""
    out = []
    for flag in ("--kata-config", "--kata-name"):
        m = re.search(rf"{flag}\s+(\S+)", cmd)
        if m:
            out += [flag, m.group(1)]
    return out


def _split_cmd(cmd):
    """把命令行拆成 [exe, arg1, ...]。引号里的路径带空格，不能简单 split"""
    parts = re.findall(r'"([^"]*)"|(\S+)', cmd)
    return [a or b for a, b in parts]


def _grab(cmd, flag, default=""):
    """取出 -flag 后面的路径

    ⚠️ 不能写成 `"?([^"]+?)"?(?:\\s|$)`（2026-10-22 实测）
    ----------------------------------------------------
    JSON 里的命令混着两种写法：
        -model "D:/WEIXIN/.../w.bin.gz"      ← 正斜杠 + 引号
        -model "D:\\WEIXIN\\...\\w.bin.gz"    ← 反斜杠 + 引号（json 已转义成 \\）
    用 [^"]+? 配 \\s 收尾时，`\\` 会被当成普通字符吃进 `[^"]`，
    但随后「引号闭合」的判断会让惰性匹配停在第一个 `\\` 之前，
    结果拿到 `...\\LizzieYzy` 这种半截路径。

    所以这里：先取引号内的完整内容；没有引号才退回按空白切。
    """
    m = re.search(rf'{flag}\s+"([^"]+)"', cmd)
    if m:
        return m.group(1).replace("\\\\", "\\")
    m = re.search(rf"{flag}\s+(\S+)", cmd)
    return m.group(1) if m else default


def _cloud_entry(d, idx):
    cmd = d["command"]
    gpu = _gpu_of(cmd)
    wt = _weight_of(cmd, d.get("name"), gpu)
    extra = _extra_of(cmd)
    key = f"cloud:{gpu}:{wt}"
    if extra:
        key += ":" + re.sub(r"[^A-Za-z0-9]", "", extra[1])
    return {
        "key": key, "name": d["name"], "kind": "cloud",
        "gpu": gpu, "weight": wt, "args": extra,
        "idx": idx,
        "note": _CLOUD_BILLING.get(gpu, ""),
        "cmd": cmd, "komi": d.get("komi", 7.5),
    }


# 本机权重文件名里的架构标记，用来生成短 key
_TAGS = ("tf3-b11c768", "zhizi-b40c768nbt", "b28c512nbt", "b18c384nbt",
         "b60c320", "b20c256x2", "lifego_b18")


def _local_entry(d, idx):
    cmd = d["command"]
    argv = _split_cmd(cmd)
    if not argv:
        return None
    name = d.get("name", "")
    model = _grab(cmd, "-model")
    cfg = _grab(cmd, "-config")
    wbase = os.path.basename(model)
    tag = ""
    for t in _TAGS:
        if t in wbase:
            tag = t
            break
    if not tag:
        tag = re.sub(r"[^a-z0-9]", "", wbase.lower())[:16] or f"w{idx}"
    is_lifego = ("Kill-all" in name) or ("死活" in name) or ("lifego" in wbase)
    kind = "lifego" if is_lifego else "local"
    komi = 20.0 if is_lifego else d.get("komi", 7.5)
    if is_lifego:
        # 老板的规矩（强调过两次）：
        # 「这个权重没有目差概念，就是黑棋把棋盘上的棋子全杀了就赢，
        #   杀不死就输」→ 死活一律 komi=20，且**绝不能看目差/scoreLead**。
        #
        # ⚠️ 文案压缩过（2026-10-23 主界面实测）
        # ----------------------------------------------------
        # 原文「死活专用 komi=20（无视目差，只看杀光/杀不光）」在引擎行
        # 要 590px，而那一行实际只有 535px —— 正好被截成「只看杀光/…」，
        # 最关键的后半句没了。
        # 界面上的字要和老板的说法一致（「杀光/杀不光」是他的原话），
        # 但「无视目差」这层意思 komi=20 + 杀光 已经包含了，
        # 去掉后仍在 535px 内，完整可读。**信息不丢，只是不重复**。
        note = "死活专用 komi=20　杀光/杀不光"
    else:
        note = _local_note(name, wbase)
    return {
        "key": f"{kind}:{tag}", "name": name, "kind": kind,
        "gpu": None, "weight": tag, "args": [],
        "idx": idx, "note": note, "cmd": cmd,
        "exe": argv[0], "argv": argv,
        "model": model, "cfg": cfg, "komi": komi,
    }


def _local_note(name, wbase):
    if "11B" in name or "b11c768" in wbase:
        return "≈14595 Elo（与云端 fdx 同权重）"
    if "60B" in name or "b60c320" in wbase:
        return "60B 大权重，显存吃紧"
    if "40B" in name or "b40c768" in wbase:
        return "≈14500 Elo"
    if "28B" in name or "b28c512" in wbase:
        return "≈14103 Elo"
    if "18B" in name or "b18c384" in wbase:
        return "≈13800 Elo"
    if "20B" in name or "b20c256" in wbase:
        return "≈13400 Elo（老权重）"
    return ""


def registry():
    """返回引擎列表，序号**重排为连续的 1..N**

    ⚠️ 为什么不再用 Lizzie 原编号（2026-10-23 老板要求）
    ----------------------------------------------------
    Lizzie 的 config.txt 里 engine-settings-list 有 30 个条目，
    其中不少是分割线 / 说明 / 老板删过的空位，所以我们筛出 16 个之后，
    编号变成 1,2,3,4,5,6,7,8,10,18,19,20,21,22,23,30 —— 中间全是洞。
    老板原话：「把里面的权重的序号整理一下，从上往下是 123456789 这样，
    现在是乱的」。

    现在：`idx` = 显示序号，从 1 开始连续编号；
    `src_idx` = Lizzie 配置里的原始序号，只留着排查用（不显示）。

    ⚠️ 顺序仍然按 Lizzie 的原始顺序排（`sorted by _idx`），
       不改变引擎的先后 —— 老板说「我已经设置好了的引擎不变」，
       只要求编号连续，没要求重排优先级。默认引擎由 default_key()
       按 key 指定，跟编号无关，所以重编号不会换掉默认项。
    """
    bs = _blocks()
    picked = []
    for d in sorted(bs, key=lambda x: x["_idx"]):
        cmd = (d.get("command") or "").strip()
        if not cmd:
            continue                          # 分割线 / 说明条目
        try:
            e = _cloud_entry(d, d["_idx"]) if _is_cloud(cmd) else _local_entry(d, d["_idx"])
        except Exception:
            e = None
        if e:
            picked.append(e)

    if not picked:
        return list(FALLBACK)

    # 重编号：1..N 连续；原序号留作 src_idx
    for n, e in enumerate(picked, 1):
        e["src_idx"] = e.get("idx")
        e["idx"] = n
    return picked


def check(reg=None):
    """自检：路径是否都存在，返回 (ok, 问题列表)"""
    reg = reg or registry()
    bad = []
    for e in reg:
        if e["kind"] == "cloud":
            continue
        # ⚠️ 跳过分割线/说明行（2026-11-01）
        # 合并后的列表里可能混进 Lizzie 配置里的「----KataGo本机引擎----」
        # 这种没有 command 的装饰条目：它既没有 exe 也没有 model，
        # 会被判成「找不到路径」→ 每次启动都在日志里报一个假故障。
        name = str(e.get("name") or "")
        if (not e.get("key") or name.startswith("---")
                or not (e.get("exe") or e.get("model"))):
            continue
        if not os.path.isfile(e.get("exe") or ""):
            bad.append(f"{e['idx']:>2}. {e['name']}：找不到 {e.get('exe')}")
        if e.get("model") and not os.path.isfile(e["model"]):
            bad.append(f"{e['idx']:>2}. {e['name']}：找不到权重 {os.path.basename(e['model'])}")
        if e.get("cfg") and not os.path.isfile(e["cfg"]):
            bad.append(f"{e['idx']:>2}. {e['name']}：找不到配置 {os.path.basename(e['cfg'])}")
    return (not bad), bad


# 配置读不到时的兜底（只放最能用的那项）
#
# ⚠️ 名字必须和引擎检测给的**完全一致**（2026-10-23）
# ----------------------------------------------------
# 同一条引擎在两处叫不同名字，用户切换来源时会以为是两个东西。
# 而且这条是新用户唯一能看到的那一项（没有 config.txt 时），
# 名字尤其要干净：不写权重、不写天梯分（老板 2026-10-23 要求）。
FALLBACK = [
    {"key": "cloud:vip-share:fdx", "name": "智子VIP共享",
     "kind": "cloud", "gpu": "vip-share", "weight": "fdx", "args": [],
     "idx": 1, "src_idx": 1, "note": "VIP 不限时", "komi": 7.5,
     "cmd": "./zz-ikatago --gpu-type vip-share --kata-weight fdx"},
]


def default_key(reg=None):
    """默认 = 序号 1（智子VIP共享）
    老板 2026-10-22：「这个是正常下棋的，不做死活题」
    """
    reg = reg or registry()
    for e in reg:
        if e.get("idx") == 1:
            return e["key"]
    for e in reg:
        if e["kind"] == "cloud" and e["gpu"] == "vip-share" and e["weight"] == "fdx":
            return e["key"]
    return reg[0]["key"] if reg else None


def strength_of(e):
    return WEIGHT_STRENGTH.get((e.get("weight") or "").lower(), 0)


def find(key, reg=None):
    """按 key / 别名 / 名字片段 / 序号 找引擎"""
    reg = reg or registry()
    if key is None:
        return None
    s = str(key).strip()
    if not s:
        return None
    # 纯数字 = Lizzie 序号，优先按 idx 精确命中
    if s.isdigit():
        n = int(s)
        for e in reg:
            if e.get("idx") == n:
                return e
        return None
    for e in reg:
        if e["key"] == s:
            return e
    low = s.lower()
    alias = {"vip": "vip-share", "free": "share"}.get(low, low)
    for e in reg:
        if alias in (e["key"].lower(), (e["weight"] or "").lower(),
                     (e["gpu"] or "").lower()):
            return e
    for e in reg:
        if low in (e["name"] or "").lower():
            return e
    return None


def pad(s, n):
    """按显示宽度补空格（中文算 2 格）"""
    s = s or ""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + " " * max(0, n - w)


def table(reg=None):
    """对齐的引擎表格（序号已是连续的 1..N）"""
    reg = reg or registry()
    dk = default_key(reg)
    rows = [(e.get("idx", 0), "*" if e["key"] == dk else " ",
             e["name"], e["key"], e["note"]) for e in reg]
    w1 = max((sum(2 if ord(c) > 0x2E80 else 1 for c in r[2]) for r in rows), default=10)
    w2 = max((len(r[3]) for r in rows), default=10)
    lines = []
    for idx, mark, nm, k, note in rows:
        lines.append(f"{idx:>2}.{mark} {pad(nm, w1)}  {pad(k, w2)}  {note}")
    return "\n".join(lines), reg


if __name__ == "__main__":
    import sys
    txt, reg = table()
    print(f"可用引擎 {len(reg)} 个（* = 默认）\n")
    print(txt)
    ok, bad = check(reg)
    print()
    if ok:
        print("路径自检：全部存在")
    else:
        print("路径自检发现问题：")
        for b in bad:
            print("  " + b)
