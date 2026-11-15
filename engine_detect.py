# -*- coding: utf-8 -*-
"""引擎自动检测 —— 扫本机 KataGo 与智子云算力

要解决什么
------------------------------------------------------------
老板原话：
    「如果能够做到自动检测电脑里的 katago 或智子围棋里面的云算力权重
      并连接就更好了」

所以这个模块只做一件事：**把本机能用的引擎全找出来**，
输出格式和 `engines.registry()` 完全一致（同一套 dict），
上层可以无差别混用。

三类来源
------------------------------------------------------------
1. **智子云算力**（`zz-ikatago.exe`）
   智子客户端（ikatago_gui_installer）装好后带这个 exe，
   它是一根 SSH 隧道，吃 GTP 命令。凭据在 `resources/data/.client`。
   检测点：exe 存在 + `.client` 存在（有凭据才算「能连」）。

2. **本机 KataGo 权重**（`katago.exe` + `*.bin.gz`）
   全盘找一个 katago.exe（常见位置优先），再在它附近 + 常见权重目录
   里找 `*.bin.gz` / `*.txt.gz`。
   检测点：exe 能执行（`version` 子命令返回 0）+ 权重文件可读。

3. **Lizzie 配置里已登记的**（`engines.registry()`）
   老板已经调好的那 16 项，原样保留，只是标注来源。

为什么不能只扫「配置文件」
------------------------------------------------------------
新用户电脑上没有 Lizzie 的 config.txt（那是智子客户端的附属品），
`engines.registry()` 会退化到 FALLBACK 只剩 1 项。
所以必须能**独立**从 exe + 权重目录推断出可用引擎 —— 这才是「自动检测」。

代价与速度
------------------------------------------------------------
全盘 os.walk 太慢（C 盘几十万文件）。策略：
  ① 先查已知路径清单（命中率最高，0 成本）
  ② 再扫「用户目录下 3 层内」+「常见安装根」的 exe
不做全盘扫描，宁可漏也不要让首次启动卡几分钟。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

import runtime_paths as RP


# ================================================================ 已知路径

def _home():
    return os.path.expanduser("~")


def _katago_candidates():
    """katago.exe 的常见位置（按命中概率排序）

    ⚠️ 这里只列**路径模板**，不保证存在 —— 存在性由调用方判断。
       顺序 = 优先尝试的顺序，找到第一个能跑的就不再往下找。

    ⚠️ 2026-10-23 修：不许出现 `C:\\Users\\Administrator\\...`
    ----------------------------------------------------
    这是分发级 bug。别人电脑上用户名不是 Administrator，
    这个候选永远不命中；更要命的是它会让**另一处**（zhizi_cloud /
    local_engine）以为"程序会自己找到"，结果谁也没找到。
    现在一律用 expanduser / LOCALAPPDATA 推当前用户。
    """
    h = _home()
    la = os.environ.get("LOCALAPPDATA", os.path.join(h, "AppData", "Local"))
    out = [
        # 标准安装位置
        os.path.join(la, "Programs", "KataGo", "katago.exe"),
        r"C:\Program Files\KataGo\katago.exe",
        r"C:\Program Files (x86)\KataGo\katago.exe",
        # 常见解压位置（当前用户，不是 Administrator）
        os.path.join(h, "Downloads", "katago.exe"),
        os.path.join(h, "Downloads", "katago-v1.18.0", "katago.exe"),
        os.path.join(h, "Desktop", "katago.exe"),
        os.path.join(h, "katago", "katago.exe"),
    ]
    # 顺手查 PATH
    p = shutil.which("katago")
    if p:
        out.insert(0, p)
    return out


def _ikatago_dirs():
    """智子客户端的实际安装目录（扫 app-* 版本目录，别写死 1.3.0）

    ⚠️ 2026-10-23：原来代码里三处都写死 `app-1.3.0`。
    客户端升级到 1.4.x 之后，那三条**全部失效**，表现是
    「明明装了智子，程序说没装」。版本号是别人的发版节奏，
    我们没法保证它永远叫 1.3.0，所以必须扫。
    """
    la = os.environ.get("LOCALAPPDATA", "") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    base = os.path.join(la, "ikatago_gui_installer")
    out = []
    try:
        names = os.listdir(base)
    except Exception:
        names = []

    def _ver(n):
        m = re.search(r"app-([\d.]+)", n)
        if not m:
            return (0,)
        return tuple(int(x) for x in m.group(1).split(".") if x.isdigit())

    for name in sorted(names, key=_ver, reverse=True):
        if name.lower().startswith("app-"):
            out.append(os.path.join(base, name, "resources", "data"))
    # 都没扫到的兜底（老版本，路径至少不会更糟）
    out.append(os.path.join(base, "app-1.3.0", "resources", "data"))
    return out


def _weight_dirs():
    """权重常见目录

    权重文件名有强特征（`kata1-` / `b18c384` / `.bin.gz`），
    所以这里多列目录是安全的 —— 扫到别的 .gz 也会被过滤器挡掉。
    """
    h = _home()
    la = os.environ.get("LOCALAPPDATA", os.path.join(h, "AppData", "Local"))
    out = [
        # 老板本机（同样是当前用户推，不是写死 Administrator）
        os.path.join(h, "Downloads", "20210116-windows64",
                     "20210116-windows64", "weights"),
        # 常见
        os.path.join(h, "Downloads", "KataGo", "weights"),
        os.path.join(h, "Downloads", "katago-v1.18.0", "weights"),
        os.path.join(h, "katago", "weights"),
        os.path.join(la, "Programs", "KataGo", "weights"),
        r"C:\Program Files\KataGo\weights",
        # 绿色便携：权重直接扔 exe 旁边（发给别人时最省事）
        os.path.join(RP.USER_DIR, "weights"),
        os.path.join(RP.HERE, "weights"),
    ]
    # 智子客户端里也可能塞了权重（版本目录扫描）
    for d in _ikatago_dirs():
        out.append(os.path.join(d, "lizzieyzy", "weights"))
    return out


def _ikatago_candidates():
    """zz-ikatago.exe（智子云隧道）的常见位置"""
    la = os.environ.get("LOCALAPPDATA", "") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    out = []
    for d in _ikatago_dirs():
        out.append(os.path.join(d, "lizzieyzy", "zz-ikatago.exe"))
        out.append(os.path.join(d, "zz-ikatago.exe"))
    out.append(os.path.join(la, "Programs", "ikatago", "zz-ikatago.exe"))
    # 绿色便携：exe 旁边放一份
    out.append(os.path.join(RP.USER_DIR, "zz-ikatago.exe"))
    p = shutil.which("zz-ikatago")
    if p:
        out.insert(0, p)
    return out


def _cred_paths(ikatago):
    """智子登录凭据报道 —— 实测三个位置都要试

    ⚠️ 2026-10-23 实测纠正
    ----------------------------------------------------
    我原先只查 `resources/data/.client`，结果 `ikatago_cred` 一直是 False，
    看起来像「老板没登录」，其实是路径错了。真实情况：

      `resources/data/lizzieyzy/.client`   ← 就是这里，12 字节
      内容：`lizzieyzy-pc`（客户端标识，不是 token）

    `.client` 只是一个「这台机器叫什么名字」的标签，
    **真正的 token 要用 `zz-ikatago get-credential token` 现读**
    （实测返回 `{"token":"c82h10..."}`）。

    所以判断「能不能连云端」要看 token，不能看 .client 存不存在。
    但读 token 要起一次进程（约 0.3~1s），首次设置向导里做一次可以接受，
    平时不该每次启动都跑 → 见 detect() 的 check_token 参数。
    """
    d = os.path.dirname(ikatago or "")
    up1 = os.path.dirname(d)            # .../resources/data
    up2 = os.path.dirname(up1)          # .../resources
    up3 = os.path.dirname(up2)          # .../app-1.3.0
    cands = [
        os.path.join(d, ".client"),         # lizzieyzy/.client  ← 实测命中
        os.path.join(up1, ".client"),
        os.path.join(up2, ".client"),
        os.path.join(up3, ".client"),
        os.path.join(os.path.expanduser("~"), ".client"),
    ]
    seen, out = set(), []
    for c in cands:
        c = os.path.abspath(c)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def read_token(ikatago, timeout=12):
    """读智子登录 token。返回 (token, 说明)

    ⚠️ 必须绕系统代理（2026-10-22 踩过）
      zz-ikatago 走的是自家 SSH 隧道，本机有代理软件时环境变量
      HTTP_PROXY 会让它连不上，表现为「明明登录了却读不到 token」。
      这里把代理变量清掉再跑。

    ⚠️ 超时给 12 秒
      首次跑可能要做一次网络探测；老板机器上实测 0.3 秒就返回，
      但慢机器/冷启动不能卡死首启向导。
    """
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    try:
        r = subprocess.run([ikatago, "get-credential", "token"],
                           capture_output=True, timeout=timeout, env=env,
                           creationflags=0x08000000)     # CREATE_NO_WINDOW
        txt = (r.stdout or b"").decode("utf-8", "ignore").strip()
        if not txt:
            txt = (r.stderr or b"").decode("utf-8", "ignore").strip()
        if not txt:
            return None, "zz-ikatago 没有输出（可能网络不通）"
        try:
            obj = json.loads(txt)
        except Exception:
            return None, f"返回内容看不懂：{txt[:80]}"
        tok = obj.get("token") or ""
        if tok:
            return tok, "已登录"
        # 有 error 字段时把它原样给人看，比自己编一句话有用
        err = obj.get("error") or obj.get("message") or txt[:120]
        return None, f"未登录（{err}）"
    except subprocess.TimeoutExpired:
        return None, f"读取凭据超时（>{timeout}s），检查网络"
    except Exception as e:
        return None, f"读取凭据失败：{type(e).__name__}: {e}"


def _lizzie_cfg_candidates():
    """Lizzie 的 config.txt（里面登记了老板调好的引擎）"""
    la = os.environ.get("LOCALAPPDATA", "")
    h = _home()
    return [
        os.path.join(la, "ikatago_gui_installer", "app-1.3.0",
                     "resources", "data", "lizzieyzy", "config.txt"),
        os.path.join(h, "AppData", "Roaming", "Lizzie", "config.txt"),
    ]


# ================================================================ 权重识别

# 权重架构标记 → (显示名, 估算 Elo, 备注)
# ⚠️ 顺序有讲究：先匹配更具体的（b11c768 要先于 b11）
# 权重架构表：tag(匹配用) / 显示名 / elo(仅内部参考，不上界面) / 备注
#
# ⚠️ 显示名只要「多大」（2026-10-23 老板要求）
# ----------------------------------------------------
# 原来是 `11B (tf3-b11c768)` 这种，括号里是**文件名片段**。
# 老板原话：「不需要介绍引擎（本机katago同理）」。
# 括号串对用户毫无意义 —— 他关心的是「这个强不强」，
# 而不是 block 数 / channel 数 / 权重代号。
# 内部匹配照样用 tag，`elo` 字段也留着（程序算默认引擎要用），
# 只是**不再显示**。
_ARCH_TABLE = (
    ("tf3-b11c768",  "11B", 14595, "与云端 fdx 同权重"),
    ("b11c768",      "11B", 14595, "与云端 fdx 同权重"),
    ("b60c320",      "60B", 14600, "大权重，显存吃紧"),
    ("b40c768",      "40B", 14500, ""),
    ("b40c256",      "40B", 14500, "老版 40B"),
    ("b28c512",      "28B", 14103, ""),
    # ⚠️ kata20bs530 这类命名（2026-10-23 实测补）
    # ----------------------------------------------------
    # 实际文件名：`kata20bs530.bin.gz`
    #   → `kata` 后面**直接跟数字**，没有 `b` 前缀。
    #   我第一版按 `b20bs` 匹配，`'b20bs' in 'kata20bs530'` 是 False
    #   （那是 `a20bs`），于是老板本机的 20B 权重被静默跳过 ——
    #   看起来就像「检测不全」。
    # `bs` = block size，是 KataGo 早期的另一种记法，架构仍是 20 block。
    ("20bs530",      "20B", 13400, "老权重"),
    ("b20bs",        "20B", 13400, "老权重"),
    ("b20c256",      "20B", 13400, "老权重"),
    ("b18c384",      "18B", 13800, ""),
    ("b15c192",      "15B", 13200, ""),
    ("b10c128",      "10B", 12800, ""),
    ("b6c96",        "6B",  11800, "轻量"),
    ("lifego_b18",   "18B 死活专用", None, "死活 Kill-all（komi=20）"),
)


def _arch_of(fname):
    """从权重文件名推断架构，返回 (短tag, 显示名, elo, 备注) 或 None

    ⚠️ 兜底正则要能认全三种常见写法（2026-10-23 补）
    ----------------------------------------------------
      kata1-b28c512nbt-s13255194368-d5935380940.bin.gz   → b28c512
      kata20bs530.bin.gz                                → b20bs530
      b18c384nbt-humanv0.bin.gz                          → b18c384
    老正则 `b(\\d+)c(\\d+)` 只认第一种和第三种，`b20bs530` 认不出来，
    于是这份权重直接从列表里消失。现在加一条 `b(\\d+)bs(\\d+)`。
    """
    low = fname.lower()
    for tag, disp, elo, note in _ARCH_TABLE:
        if tag in low:
            return tag, disp, elo, note
    # 兜底：kata1-bXXcYYY 形式
    m = re.search(r"b(\d+)c(\d+)", low)
    if m:
        b, c = m.group(1), m.group(2)
        return f"b{b}c{c}", f"{b}B (b{b}c{c})", None, ""
    # 兜底：kataXXbsYYY 形式（早期 block-size 记法，注意没有 b 前缀）
    m = re.search(r"(\d+)bs(\d+)", low)
    if m:
        b, s = m.group(1), m.group(2)
        return f"{b}bs{s}", f"{b}B ({b}bs{s})", None, ""
    return None


def _is_killall(fname):
    return "lifego" in fname.lower()


# ================================================================ 检测主入口

def _entry_cloud(gpu, weight, name, note, args=None):
    """构造一条云引擎条目（结构同 engines._cloud_entry）"""
    key = f"cloud:{gpu}:{weight}"
    if args:
        key += ":" + re.sub(r"[^A-Za-z0-9]", "", args[1])
    return {
        "key": key, "name": name, "kind": "cloud",
        "gpu": gpu, "weight": weight, "args": args or [],
        "idx": 0, "note": note, "komi": 7.5,
        "src": "detect",         # 来源标记：detect=自动检测
    }


def _entry_local(exe, model, cfg, name, note, komi=7.5, kind=None):
    """构造一条本机引擎条目（结构同 engines._local_entry）"""
    wbase = os.path.basename(model)
    arch = _arch_of(wbase)
    tag = arch[0] if arch else re.sub(r"[^a-z0-9]", "", wbase.lower())[:16]
    is_ka = _is_killall(wbase) or (kind == "lifego")
    k = "lifego" if is_ka else "local"
    return {
        "key": f"{k}:{tag}", "name": name, "kind": k,
        "gpu": None, "weight": tag, "args": [],
        "idx": 0, "note": note,
        "exe": exe, "argv": [exe, "analysis",
                             "-model", model, "-config", cfg],
        "model": model, "cfg": cfg,
        "komi": 20.0 if is_ka else komi,
        "src": "detect",
    }


def _katago_usable(exe):
    """katago.exe 能不能跑（`version` 子命令）

    ⚠️ 为什么要真跑一次（2026-10-23）
    ------------------------------------
    exe 存在不等于能用：可能是 32 位、可能缺 DLL、可能是个空壳。
    真跑 `version` 只要 50ms，能一次性排除全部情况。
    超时给 8 秒 —— 老机器冷启动读盘可能慢。
    """
    try:
        r = subprocess.run([exe, "version"], capture_output=True,
                           timeout=8, creationflags=0x08000000)  # CREATE_NO_WINDOW
        return r.returncode == 0
    except Exception:
        return False


def _cfg_for(exe_dir):
    """给检测到的 katago 配一个 cfg

    优先用随程序带的那份（`engine/analysis_fast.cfg`，已按对弈调好、
    且显式写了 reportAnalysisWinratesAs=WHITE）。
    找不到就退回 katago 自带的 analysis_example.cfg。
    """
    ours = os.path.join(RP.HERE, "engine", "analysis_fast.cfg")
    if os.path.isfile(ours):
        return ours
    for cand in ("analysis_example.cfg", "analysis.cfg", "gtp_example.cfg"):
        p = os.path.join(exe_dir, cand)
        if os.path.isfile(p):
            return p
    return None


def merged(reg_lizzie=None, det=None, extra=None):
    """把三类来源合成**一份**引擎列表 —— 主界面和设置向导都必须用它

    ⚠️ 为什么必须有这个函数（2026-10-23 实测抓到的真 bug）
    ----------------------------------------------------
    我先让向导展示 `detect()` 的结果、主界面用 `engines.registry()`，
    结果两边算出来的 key 不一样：
        detect 给本机权重算 key  →  `local:b18c384`
        Lizzie 配置里的同一份权重 →  `local:b18c384nbt`
    于是向导里勾了 8 项，主界面筛完只剩 4 项（只有云端的 key 恰好一致）。
    用户看到的就是「我明明勾了，怎么少了一半」。

    根治办法不是去改 key 规则（那会动到已经存进 settings.json 的旧数据），
    而是**让两边用同一份列表**：启动时合并一次，向导传这份、主界面用这份，
    key 天然一致，再也不用对齐。

    顺序（重要）
    ----------------------------------------------------
    ① Lizzie 里已登记的 —— 老板钦定，**原样置顶、不改任何字段**
    ② 自动检测到的云端 —— 按 key 去重
    ③ 自动检测到的本机权重 —— 按**权重文件路径**去重
       （不用 key：同一个文件在不同来源里 tag 可能写得不一样，
         但路径一定相同，这是唯一可靠的同一性判据）
    ④ 手动添加的 —— 按 key + 路径去重
    """
    out = []
    seen_key = set()
    seen_model = set()

    def _add(e):
        if not isinstance(e, dict) or not e.get("key"):
            return False
        k = e["key"]
        m = os.path.normcase(os.path.abspath(e["model"])) if e.get("model") else ""
        if k in seen_key:
            return False
        if m and m in seen_model:
            return False
        seen_key.add(k)
        if m:
            seen_model.add(m)
        out.append(e)
        return True

    # ① Lizzie（老板那 16 项；新用户这里可能只有 FALLBACK 1 项）
    for e in (reg_lizzie or []):
        e = dict(e)
        e.setdefault("src", "lizzie")
        _add(e)

    # ②③ 自动检测
    if det:
        for e in (det.get("clouds") or []):
            _add(dict(e))
        for e in (det.get("locals") or []):
            _add(dict(e))

    # ④ 手动
    for e in (extra or []):
        _add(dict(e))

    # 重编号 1..N —— 老板要的「从上往下 123456789」
    for n, e in enumerate(out, 1):
        e["src_idx"] = e.get("src_idx", e.get("idx"))
        e["idx"] = n
    return out


def detect(deep=False, progress=None, check_token=True):
    """扫一遍本机，返回检测结果

    参数
    ----
    deep         是否额外扫「用户目录下 3 层」里的 katago.exe（慢，2~10s）
    progress     回调 progress(str)，用于界面上报进度
    check_token  是否真去读智子登录 token（会起一次子进程，0.3~12s）
                 首次设置向导传 True；平时启动传 False 省时间

    返回
    ----
    dict:
        clouds   [entry]   智子云算力（含凭据检测结果）
        locals   [entry]   本机 KataGo（每份权重一条）
        lizzie   [entry]   Lizzie 配置里已登记的（老板调好的那批）
        info     dict      环境信息：katago 路径 / 权重数 / 智子状态
        problems [str]     给人看的问题说明
    """
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    res = {"clouds": [], "locals": [], "lizzie": [],
           "info": {}, "problems": []}

    # ---------------------------------------------------------- 1) 智子云
    say("检测智子云算力 …")
    ikatago = None
    for p in _ikatago_candidates():
        if os.path.isfile(p):
            ikatago = p
            break
    res["info"]["ikatago"] = ikatago

    if ikatago:
        # ⚠️ 判断「能不能连云端」看的是 **token**，不是 .client 文件
        #    （2026-10-23 实测纠正：.client 只是客户端标识）
        cred_file = None
        for c in _cred_paths(ikatago):
            if os.path.isfile(c):
                cred_file = c
                break
        res["info"]["ikatago_cred_file"] = cred_file

        token, why = (None, "未检测")
        if check_token:
            say("读取智子登录状态 …")
            token, why = read_token(ikatago)
        else:
            # 不读 token 时：有 .client 就当他登录过（乐观），
            # 真正的判定留给「开始下棋」那一刻的错误信息
            token = "skipped" if cred_file else None
            why = "有客户端标识（未验证登录）" if cred_file else "未登录"
        res["info"]["ikatago_token_ok"] = bool(token)
        res["info"]["ikatago_cred"] = bool(token)   # 兼容老字段名
        res["info"]["ikatago_cred_reason"] = why
        has_cred = bool(token)

        if not has_cred:
            res["problems"].append(
                f"找到智子云隧道（zz-ikatago.exe），但登录状态是「{why}」。"
                f"打开智子围棋客户端登录一次即可。")

        # 云档位：与 engines._CLOUD_BILLING 对齐
        #
        # ⚠️⚠️ 名称只写「智子客户端里本来就叫的名字」（2026-10-23 老板要求）
        # ----------------------------------------------------
        # 老板原话：
        #   「智子里面的引擎名称就只有智子VIP共享，没有什么权重和天梯分数，
        #     这是我自己配的，对于新用户你也不用写，你就写最干净的引擎名称
        #     就行，也不需要介绍引擎（本机katago同理）」
        #
        # 原来这里写的是 `智子VIP算力 11b (fdx)`：
        #   · `11b` / `fdx` 是**权重型号**，老板在 Lizzie 里自己配的名字
        #   · 智子客户端自己的界面上根本没有这两个东西
        #   · 新用户看到「11b (fdx)」，既不知道是什么，也没得选（他只有一档）
        # 这是典型的「把开发者的内部视角泄漏到用户界面」——
        # 对我有用（排查时要按权重区分），对用户是噪音。
        #
        # 现在：名字 = 客户端里的叫法；括号里的技术串全删。
        # 权重仍然记在条目的 `weight` 字段里（程序要用），只是**不显示**。
        #
        # note 只留**要花钱**的信息（元/小时）——
        # 那不是"引擎介绍"，是用户选档位时必须知道的代价。
        # 「与云端 fdx 同权重」「大权重，显存吃紧」这类技术说明全部去掉。
        tiers = [
            ("vip-share", "fdx", "智子VIP共享", "VIP 不限时"),
            ("1x",  "fdx", "智子 1x",  "约 0.8~1.5 元/小时"),
            ("3x",  "fdx", "智子 3x",  "2.4 元/小时"),
            ("6x",  "fdx", "智子 6x",  "4.8 元/小时"),
            ("12x", "fdx", "智子 12x", "9.6 元/小时"),
            ("24x", "fdx", "智子 24x", "19.2 元/小时"),
        ]
        for gpu, wt, nm, note in tiers:
            e = _entry_cloud(gpu, wt, nm, note)
            e["usable"] = has_cred
            if not has_cred:
                e["note"] = note + " · 未登录"
            res["clouds"].append(e)
    else:
        res["info"]["ikatago_cred"] = False
        res["info"]["ikatago_cred_file"] = None
        res["info"]["ikatago_token_ok"] = False
        res["info"]["ikatago_cred_reason"] = "没找到 zz-ikatago.exe"
        res["problems"].append(
            "没找到智子云隧道（zz-ikatago.exe）。想用云端算力，"
            "请先安装智子围棋客户端并登录。")

    # ---------------------------------------------------------- 2) 本机
    say("检测本机 KataGo …")
    katago = None
    cands = list(_katago_candidates())
    if deep:
        say("深度扫描 katago.exe（可能要几秒）…")
        h = _home()
        for root, dirs, files in os.walk(h):
            depth = root[len(h):].count(os.sep)
            if depth > 3:
                dirs[:] = []
                continue
            # 跳过体积巨大的无关目录，否则一分钟都跑不完
            dirs[:] = [d for d in dirs if d.lower() not in (
                "node_modules", ".git", "appdata", "__pycache__",
                ".cache", "windows", "program files", "program files (x86)")]
            for f in files:
                if f.lower() == "katago.exe":
                    cands.append(os.path.join(root, f))

    for p in cands:
        if os.path.isfile(p) and _katago_usable(p):
            katago = p
            break
    res["info"]["katago"] = katago

    if katago:
        exe_dir = os.path.dirname(katago)
        cfg = _cfg_for(exe_dir)
        res["info"]["katago_cfg"] = cfg
        if not cfg:
            res["problems"].append(
                "找到 katago.exe，但没找到可用的 analysis 配置（.cfg）。"
                "请把随软件的 engine/analysis_fast.cfg 放回原位置。")

        # 权重来源：exe 同级 + 同级 weights/ + 已知目录
        wdirs = []
        for d in (exe_dir, os.path.join(exe_dir, "weights")):
            if os.path.isdir(d):
                wdirs.append(d)
        for d in _weight_dirs():
            if os.path.isdir(d) and os.path.abspath(d) not in [
                    os.path.abspath(x) for x in wdirs]:
                wdirs.append(d)

        seen = set()
        ws = []
        say("扫描权重文件 …")
        for d in wdirs:
            for pat in ("*.bin.gz", "*.txt.gz", "*.bin"):
                for f in sorted(glob.glob(os.path.join(d, pat))):
                    real = os.path.abspath(f)
                    if real in seen:
                        continue
                    seen.add(real)
                    ws.append(real)

        res["info"]["weights"] = ws
        res["info"]["weight_dirs"] = wdirs

        if cfg:
            for w in ws:
                arch = _arch_of(os.path.basename(w))
                if not arch:
                    continue      # 认不出架构的文件不列（多半不是 KataGo 权重）
                tag, disp, elo, note = arch
                is_ka = _is_killall(w)
                # ⚠️ 本机权重的名字也只留「多大」（2026-10-23 老板要求）
                # ----------------------------------------------------
                # 老板：「本机 katago 同理」——不需要介绍引擎。
                #
                # 原来生成的是 `本机 · 18B (b18c384nbt)`，note 里还塞
                # `≈13800 Elo`。两个问题：
                #   · `(b18c384nbt)` 是文件里的架构串，用户不需要
                #   · `≈13800 Elo` 是**估算**（不是实测），挂在界面上
                #     等于给用户一个假精度；而且 Elo 再高他也没得挑，
                #     这台机器上就那几份权重，只能用有的
                # 于是：名字 = `本机 · 18B`，note 只留死活规则这种
                # **会改变下法**的信息（komi=20 杀光）。
                nm = f"本机 · {disp}"
                notes = []
                if is_ka:
                    # 死活是**另一套规则**，不说清楚用户会以为下错了
                    notes.append("死活专用 komi=20　杀光/杀不光")
                res["locals"].append(_entry_local(
                    katago, w, cfg, nm, "　".join(notes),
                    kind="lifego" if is_ka else None))

        if not res["locals"]:
            res["problems"].append(
                "找到 katago.exe，但附近没扫到可用权重（*.bin.gz）。"
                "请在「引擎设置」里手动指定权重文件。")
    else:
        res["info"]["katago"] = None
        res["info"]["weights"] = []
        res["problems"].append(
            "没找到能运行的 katago.exe。想用本机算力，"
            "请在「引擎设置」里手动指定 katago.exe 路径。")

    # ---------------------------------------------------------- 3) Lizzie
    say("读取已登记的引擎 …")
    try:
        import engines as REG
        reg = REG.registry()
        for e in reg:
            e = dict(e)
            e["src"] = "lizzie"
            # ⚠️⚠️ 这里原来是 `e["usable"] = True` 无条件放行（2026-10-23 修）
            # ----------------------------------------------------
            # 裸机实测抓到的真 bug：一个**连 zz-ikatago.exe 都没有**的
            # 机器上，向导里 9 个云端项全部打勾、全部可点。
            #
            # 为什么会这样：
            #   ① `engines.registry()` 读到 config.txt（只要有一份在，就 16 项）
            #   ② 这 16 项里 9 个是云端
            #   ③ 这里无条件标成 usable=True
            #   ④ merged() 把 Lizzie 放最前，key 优先命中
            #   ⑤ `_judge` 看到 usable=True → 可勾选
            # 于是软件在**明确知道自己连不上云**的情况下，
            # 把所有云端引擎都推给了用户。点「完成」→ 写进配置 →
            # 主界面全是连不上的 → 第一次用就失败。
            #
            # 现在按类型分别判：
            #   云端 → 必须真的找到 zz-ikatago.exe 才算能用
            #          （token 这次没读没关系，那是"没验"不是"不行"）
            #   本机 → exe / 权重 / cfg 三个文件都得在
            kind = e.get("kind")
            if kind == "cloud":
                e["usable"] = bool(ikatago)
                if not ikatago:
                    e["note"] = (e.get("note") or "") + " · 没装智子客户端"
            else:
                bad = []
                if e.get("exe") and not os.path.isfile(e["exe"]):
                    bad.append("exe")
                if e.get("model") and not os.path.isfile(e["model"]):
                    bad.append("权重")
                if e.get("cfg") and not os.path.isfile(e["cfg"]):
                    bad.append("配置")
                e["usable"] = not bad
                if bad:
                    e["missing"] = bad
            res["lizzie"].append(e)
        res["info"]["lizzie_count"] = len(reg)
    except Exception as ex:
        res["info"]["lizzie_count"] = 0
        res["problems"].append(f"读 Lizzie 引擎列表失败：{type(ex).__name__}: {ex}")

    # ---------------------------------------------------------- 4) 合并
    # ⚠️ 必须在这里就把三份合成一份（2026-10-23）
    # 上层（主界面下拉框 / 设置向导）全都读 `merged`，不要各自再拼一次。
    # 否则同一份权重会出现两个 key（local:b18c384 / local:b18c384nbt），
    # 向导勾了主界面却筛不出来。
    res["merged"] = merged(res["lizzie"], res)
    return res


def summary(res):
    """把检测结果压成一句人话（写在界面上）"""
    n_c = len(res["clouds"])
    n_l = len(res["locals"])
    n_z = len(res["lizzie"])
    parts = []
    parts.append(f"智子云 {n_c} 档" if n_c else "智子云 未检测到")
    parts.append(f"本机权重 {n_l} 份" if n_l else "本机权重 未检测到")
    if n_z:
        parts.append(f"已登记引擎 {n_z} 项")
    return "　·　".join(parts)


if __name__ == "__main__":
    print("=== 自动检测（不深度扫描，读登录 token）===")
    r = detect(progress=lambda m: print("  …", m))
    print()
    print(summary(r))
    print()
    for k in ("ikatago", "ikatago_cred_file", "ikatago_token_ok",
              "ikatago_cred_reason", "katago", "katago_cfg", "lizzie_count"):
        print(f"{k:>20}: {r['info'].get(k)}")
    ws = r["info"].get("weights") or []
    print(f"{'weights':>20}: {len(ws)} 份")
    for w in ws:
        print("      ", os.path.basename(w))
    print()
    print(f"云端 {len(r['clouds'])} 条：")
    for e in r["clouds"]:
        print(f"   {e['name']:<28} {e['note']}")
    print(f"本机 {len(r['locals'])} 条：")
    for e in r["locals"]:
        print(f"   {e['name']:<28} {e['note']}")
    print()
    if r["problems"]:
        print("问题：")
        for p in r["problems"]:
            print("  -", p)
