# -*- coding: utf-8 -*-
"""用户设置（settings.json）—— 记住「首次设置做完了没」「用户选了哪些引擎」

要解决什么
------------------------------------------------------------
老板 2026-10-23 原话：
    「用户安装这个软件之后，会有一个初始设置，让用户配置电脑里的
      katago 权重或者连接智子围棋里面的云算力，当然我已经设置好了的
      引擎不变」

所以需要一份**持久化**的偏好：
  · `setup_done`   首次设置向导做过没有（没做过 → 启动就弹）
  · `enabled`      用户勾选了哪些引擎的 key（老板自己那 16 项默认全开）
  · `default_key`  默认选哪个
  · `extra`        用户手动「浏览…」加进来的引擎（路径是绝对路径）

⚠️ 存哪：`RP.USER_DIR/settings.json`
   源码态 = 项目目录；打包态 = exe 旁边。跟 logs/ 同一个原则：
   绿色单文件，拷到哪设置就跟到哪。

⚠️ 为什么所有读写都要 try/except 兜住
   这份文件是**用户可编辑**的（老板可能手动改），也可能是老版本写的、
   或者写到一半断电变成半截 JSON。任何一种情况都**不能**让程序启动失败 ——
   最差也得退回默认值把界面开起来，让用户能进设置改回去。
"""
import json
import os
import time

import runtime_paths as RP

# 设置文件名
FILENAME = "settings.json"

# 当前 schema 版本。将来字段变了靠它做迁移，不要靠猜。
SCHEMA = 1

# 一份朴素但完整的默认值。**注意 enabled 是 None 而不是 []** ——
# None 表示「用户没选过，该全开」；[] 表示「用户明确一个都不要」，
# 两者语义完全不同。用 [] 当默认会导致新用户第一次进来一个引擎都没有。
DEFAULTS = {
    "schema": SCHEMA,
    "setup_done": False,
    # ⚠️ 「跳过」和「配好」是两件事（2026-10-23）
    # setup_done=1     真的配好了，启动不再弹向导
    # setup_skipped=1  用户点过「稍后再说」，但**不代表配好了**
    # 老版本把「跳过」直接写成 setup_done=True，结果新电脑上
    # 用户一跳过就再也见不到向导，永久卡在空下拉框里。
    # 现在只记 skipped，是否还会弹由 gui 那边按「有没有可用引擎」决定。
    "setup_skipped": False,
    "enabled": None,          # None = 全开（老板那 16 项）
    "default_key": None,      # None = 用 engines.default_key()
    "extra": [],              # 手动添加的引擎条目
    "ui": {
        "color": "白",        # 我执
        "komi": "7.5",
        "rules": "中国规则",
        "aggro": 0,
        "think_mode": "fixed",
        "think": "5",
        "think_min": "1",
        "think_max": "10",
        "visits": "10000",
    },
    "window": {"global_esc": True},
}


def path():
    return os.path.join(RP.USER_DIR, FILENAME)


# ---------------------------------------------------------------- 读写

def load():
    """读设置。任何异常都退回默认值，**绝不抛**。

    返回 (data, 说明)。说明是 "" 表示正常读到；
    非空时是一句人话（给日志用，比如「配置文件坏了，已用默认值」）。
    """
    p = path()
    if not os.path.isfile(p):
        return _clone(DEFAULTS), ""

    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        return _clone(DEFAULTS), f"设置文件内容坏了（{e.msg}），已用默认值"
    except Exception as e:
        return _clone(DEFAULTS), f"设置文件读不出来（{type(e).__name__}），已用默认值"

    if not isinstance(raw, dict):
        return _clone(DEFAULTS), "设置文件不是对象，已用默认值"

    # 浅合并：缺的字段补默认，多的字段原样保留（向前兼容）
    data = _clone(DEFAULTS)
    for k, v in raw.items():
        if k == "ui" and isinstance(v, dict):
            data["ui"].update(v)
        elif k == "window" and isinstance(v, dict):
            data["window"].update(v)
        else:
            data[k] = v

    note = ""
    if data.get("schema") != SCHEMA:
        # 老版本：保留用户能认的字段，其余用默认。不去猜老格式细节。
        note = f"设置来自旧版本（schema={data.get('schema')}），已升级"
        data["schema"] = SCHEMA
    return data, note


def _looks_like_scripted_run():
    """判断「这次是不是脚本/测试在跑」，是的话不许写真实配置

    ⚠️ 为什么要在 save() 里拦（2026-10-23 真事故）
    ----------------------------------------------------
    已经发生过一次：测试脚本 `import gui` → App.__init__ → 某个用例调了
    `_save_ui_state()` → 真实 settings.json 被写成 `enabled: []`
    （含义是「一个引擎都不要」）→ 老板下次打开发现引擎下拉框是空的。

    第一版修法是「测试开头调 _sandbox.activate() 设 WEIQI_HOME」。
    那只管住了**写了 _sandbox 的那几个**测试。我后来随手写个验证脚本
    `python -c "import gui; gui.App(root)"`，它根本不知道 _sandbox 存在，
    于是又把配置写坏了 —— 同一个坑踩两次。

    结论：靠「调用方自觉」是守不住的，必须在写盘这个**唯一的出口**上拦。

    判据（保守，宁可多拦）：
      ① 设了 WEIQI_HOME → 明确是沙箱，允许写（写到沙箱里）
      ② 解释了器带 -c / -m unittest / pytest → 脚本跑，拦
      ③ 主脚本名以 _ 或 test 开头（_t5a_gui.py / test_xxx.py）→ 拦

    ⚠️ 不强拦「名字像测试」的所有情况：老板双击的启动器是 .bat →
       gui.py，主脚本名是 gui.py，正常放行。
    """
    if os.environ.get("WEIQI_HOME"):
        return False                # 沙箱，放心写
    if os.environ.get("WEIQI_ALLOW_SAVE"):
        return False                # 显式放行（万一有正当用途）
    import sys
    argv = list(getattr(sys, "argv", []) or [])
    if "-c" in argv or "-m" in argv:
        # python -c "..." / python -m pytest
        return True
    main = argv[0] if argv else ""
    base = os.path.basename(main).lower()
    if base.startswith("_") or base.startswith("test_"):
        return True
    if any(k in base for k in ("pytest", "unittest")):
        return True
    return False


def save(data):
    """写设置。返回 (ok, 说明)。

    ⚠️ 先写临时文件再 os.replace（原子替换）
    ----------------------------------------------------
    直接 open(p,'w') 覆盖有个窗口期：写到一半程序被杀 / 断电 →
    留下半截 JSON → 下次启动读到就报废。
    os.replace 在 Windows 上也是原子的（MoveFileEx + REPLACE_EXISTING），
    所以要么是完整的新文件，要么还是完整的旧文件，不会有中间态。

    ⚠️ 脚本态拒绝写真实配置（2026-10-23，见 _looks_like_scripted_run）
    """
    if _looks_like_scripted_run():
        return False, "（脚本态：不写真实配置，避免把设置改坏）"
    p = path()
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = dict(data or {})
        data["schema"] = SCHEMA
        data["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True, ""
    except Exception as e:
        # 清掉可能的残留临时文件，免得下次误读
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, f"设置保存失败：{type(e).__name__}: {e}"


def _clone(d):
    """深拷贝一份默认值（DEFAULTS 里嵌了 dict，浅 copy 会串改）"""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = dict(v)
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- 便捷封装

def is_setup_done():
    data, _ = load()
    return bool(data.get("setup_done"))


def mark_setup_done(extra=None):
    """把「首次设置已完成」写进去。

    ⚠️ setup_done 一旦写成 True 就**不再弹向导**（除非用户手动点「设置」）。
       所以这个函数只在向导里点「完成」时调，不别处调。
    """
    data, _ = load()
    data["setup_done"] = True
    if extra:
        data.update(extra)
    return save(data)


def save_engines(enabled_keys, default_key=None, extra=None):
    """记住用户勾选的引擎

    enabled_keys  None = 全开；list = 只开这些 key
    """
    data, _ = load()
    data["enabled"] = None if enabled_keys is None else list(enabled_keys)
    if default_key is not None:
        data["default_key"] = default_key
    if extra is not None:
        data["extra"] = list(extra)
    data["setup_done"] = True
    return save(data)


def filter_engines(reg, data=None):
    """按设置筛一遍引擎列表；**保底至少留 1 项**

    ⚠️ 为什么必须保底（2026-10-23 想清楚的一个坑）
    ----------------------------------------------------
    老板把引擎全取消勾选、或者他勾的那些路径后来被删了 →
    筛完 reg 是空列表 → 界面下拉框空的 → 点「开启引擎」直接崩。
    宁可回退成「全开」，也不能让程序处于无引擎可用状态。
    返回 (list, 说明)。
    """
    if not reg:
        return list(reg), ""
    data = data if data is not None else load()[0]
    en = data.get("enabled")
    if en is None:
        return list(reg), ""

    want = set(str(x) for x in en)
    picked = [e for e in reg if e.get("key") in want]
    if not picked:
        return list(reg), "设置里勾选的引擎都不在了，已回退为全部"

    # 手动添加的引擎（路径型）追加在后面
    extra = data.get("extra") or []
    have = {e.get("key") for e in picked}
    for e in extra:
        if isinstance(e, dict) and e.get("key") and e["key"] not in have:
            picked.append(e)
            have.add(e["key"])
    return picked, ""


def default_key(reg, data=None):
    """决定默认选哪个引擎

    优先级：
      ① 设置里记的 default_key（用户上次选的 / 向导里选的），且它还在列表里
      ② 否则取**列表第一项**

    ⚠️ 为什么不在这里调 engines.default_key()（2026-10-23 澄清）
    ----------------------------------------------------
    本模块只做「按用户偏好挑」，不认识引擎语义，也不该反向依赖
    engines 模块（那会形成 settings → engines → settings 的循环）。
    `engines.default_key()` 保证的是「registry() 的第 1 项 = 老板钦定的
    云端 fdx」，而 merged() 已经把 registry 的结果置顶，所以
    「列表第一项」与它等价。调用方（gui._fill_engines）在拿到 None 时
    才去问 engines.default_key() 兜底，分工是清楚的。
    """
    data = data if data is not None else load()[0]
    want = data.get("default_key")
    keys = [e.get("key") for e in (reg or [])]
    if want and want in keys:
        return want
    if keys:
        return keys[0]
    return None


if __name__ == "__main__":
    print("配置文件:", path())
    d, note = load()
    print("读到的设置:", json.dumps(d, ensure_ascii=False, indent=2))
    if note:
        print("说明:", note)
    ok, msg = save(d)
    print("保存:", ok, msg)
