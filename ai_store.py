# -*- coding: utf-8 -*-
"""AI 托管的配置文件（``ai.json``）读写 + Windows DPAPI 加密

这份文件存什么
------------------------------------------------------------
只有两块：``llm``（base_url / model / api_key）和 ``host``（托管策略）。
键名与默认值以 ``AI-INTEGRATION.md`` 为准，改接口先改契约。

三条硬规则
------------------------------------------------------------
1. **api_key 不落明文**：写盘前用 DPAPI 加密成 ``llm.api_key_enc``，
   文件里没有 ``llm.api_key`` 这个键；读盘时解密回 ``llm.api_key``
   给上层用。DPAPI 不可用（非 Windows / 系统策略限制）时退回
   ``plain:<base64>``，功能不中断，但**至少不是一眼能看懂的明文**。
2. **不炸**：文件不存在、JSON 坏了、顶层不是对象，一律返回默认值；
   坏文件复制一份 ``ai.json.bad-时间`` 留证，不直接覆盖用户数据。
3. **原子写**：先写同目录 ``ai.json.tmp``，``fsync`` 后 ``os.replace``。
   写到一半断电/被杀，要么是完整的旧文件，要么是完整的新文件。

``FOXAI_HOME`` 环境变量可以覆盖目录（单测用它隔离，绝不碰真实配置）。
"""

from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import shutil
import time
from collections.abc import Mapping
from typing import Any

__all__ = [
    "DEFAULT_SETTINGS",
    "ai_home",
    "ai_path",
    "dpapi_protect",
    "dpapi_unprotect",
    "load_ai",
    "save_ai",
]

#: 默认设置（AI-INTEGRATION.md「ai_store.py」一节逐字对齐）
DEFAULT_SETTINGS: dict[str, Any] = {
    "llm": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "",
        "model": "doubao-seed-1-6-vision-250815",
        "vision": True,
    },
    "host": {
        "accept_invites": True,
        "chat_auto": True,
        "bet_auto": False,
        "bet_max_single": 10000,
        "bet_max_daily": 50000,
        "bet_min_edge": 0.0,
        "allow_ranked": False,
        "chat_style": "自然、礼貌、偶尔幽默",
    },
}

#: DPAPI 的 ``CRYPTPROTECT_UI_FORBIDDEN``：后台线程加密时绝不弹窗
CRYPTPROTECT_UI_FORBIDDEN = 0x1

#: 配置文件名
FILENAME = "ai.json"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def _default_home() -> str:
    """没设 ``FOXAI_HOME`` 时的目录：跟项目其它用户数据放一起。

    源码态是项目目录，打包态是 ``runtime_paths.USER_DIR``
    （%LOCALAPPDATA%\\野狐自动对弈 或便携模式目录）。这里延迟导入，
    避免 ai_store 被单独 import 时牵扯运行环境的初始化。
    """
    try:
        import runtime_paths as RP
        return str(RP.USER_DIR)
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def ai_home() -> str:
    """AI 数据目录。``FOXAI_HOME`` 优先（单测隔离用），不存在就建。"""
    env = (os.environ.get("FOXAI_HOME") or "").strip()
    if env:
        home = os.path.abspath(os.path.expanduser(env))
    else:
        home = _default_home()
    try:
        os.makedirs(home, exist_ok=True)
    except OSError:
        # 建不了也先返回路径，后面 load/save 会各自兜住异常
        pass
    return home


def ai_path() -> str:
    """``<home>/ai.json``"""
    return os.path.join(ai_home(), FILENAME)


# --------------------------------------------------------------------------
# 深合并（只认 dict，list/标量整体替换）
# --------------------------------------------------------------------------
def _deep_merge(base: Mapping[str, Any],
                override: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = copy.deepcopy(dict(base))
    if not override:
        return out
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# --------------------------------------------------------------------------
# DPAPI（ctypes 直调 crypt32，不装 pywin32）
# --------------------------------------------------------------------------
class _DATA_BLOB(ctypes.Structure):
    """与 Win32 ``DATA_BLOB`` 对齐（cbData + 指针）。"""

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


# 缓存动态库句柄；None 表示「试过了，不可用」
_DPAPI_API: tuple[Any, Any] | None = None
_DPAPI_LOADED = False


def _load_dpapi() -> tuple[Any, Any] | None:
    """加载 crypt32/kernel32 并声明函数签名。失败返回 None（走 plain 兜底）。"""
    global _DPAPI_API, _DPAPI_LOADED
    if _DPAPI_LOADED:
        return _DPAPI_API
    _DPAPI_LOADED = True

    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        blob_ptr = ctypes.POINTER(_DATA_BLOB)
        crypt32.CryptProtectData.argtypes = [
            blob_ptr, wintypes.LPCWSTR, blob_ptr, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, blob_ptr,
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            blob_ptr, ctypes.POINTER(wintypes.LPWSTR), blob_ptr,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_ptr,
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        _DPAPI_API = (crypt32, kernel32)
    except Exception:
        _DPAPI_API = None
    return _DPAPI_API


def _dpapi_protect_bytes(data: bytes) -> bytes | None:
    """DPAPI 加密一段 bytes；不可用/失败返回 None（由上层退 plain）。"""
    api = _load_dpapi()
    if api is None:
        return None
    crypt32, kernel32 = api
    try:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob_in = _DATA_BLOB(
            len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _dpapi_unprotect_bytes(data: bytes) -> bytes | None:
    """DPAPI 解密一段 bytes；失败返回 None。"""
    api = _load_dpapi()
    if api is None:
        return None
    crypt32, kernel32 = api
    try:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob_in = _DATA_BLOB(
            len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def dpapi_protect(text: str) -> str:
    """加密 key：成功 ``"dpapi:<base64>"``，失败 ``"plain:<base64>"``。

    ``plain:`` 是**降级方案**不是加密：只保证文件里不是一眼能读出的
    明文（也能安全放进 JSON），换机器/换用户当然解不开。空串直接返回
    空串，不给「没配 key」造一个假密文。
    """
    text = str(text or "")
    if not text:
        return ""
    raw = text.encode("utf-8")
    protected = _dpapi_protect_bytes(raw)
    if protected is None:
        return "plain:" + base64.b64encode(raw).decode("ascii")
    return "dpapi:" + base64.b64encode(protected).decode("ascii")


def dpapi_unprotect(blob: str) -> str:
    """解密 ``dpapi:`` / ``plain:`` 前缀的串；认不出/解不开返回空串。"""
    blob = str(blob or "")
    if not blob:
        return ""
    if blob.startswith("plain:"):
        try:
            return base64.b64decode(blob[len("plain:"):]).decode("utf-8")
        except (ValueError, UnicodeError):
            return ""
    if blob.startswith("dpapi:"):
        try:
            raw = base64.b64decode(blob[len("dpapi:"):])
        except (ValueError, TypeError):
            return ""
        if not raw:
            return ""
        out = _dpapi_unprotect_bytes(raw)
        if out is None:
            return ""
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    # 未知前缀：宁可当「没配」，也不把密文当 api_key 发出去
    return ""


# --------------------------------------------------------------------------
# 读 / 写
# --------------------------------------------------------------------------
def _backup_bad_file(path: str) -> str:
    """把坏文件复制成 ``<path>.bad-YYYYmmdd-HHMMSS``，返回副本路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = f"{path}.bad-{stamp}"
    suffix = 1
    while os.path.exists(target):
        target = f"{path}.bad-{stamp}-{suffix}"
        suffix += 1
    try:
        shutil.copy2(path, target)
        return target
    except OSError:
        return ""


def load_ai() -> dict:
    """读 ``ai.json``，缺省深合并；``api_key_enc`` 解密到 ``llm.api_key``。

    **任何异常都不抛**：最差返回一份默认值，让 AI 页能正常打开并提示
    用户重新填。坏文件复制一份 ``.bad-时间`` 留证。
    """
    data = copy.deepcopy(DEFAULT_SETTINGS)
    path = ai_path()
    if not os.path.isfile(path):
        return data

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception:
        # JSON 坏了 / 编码坏了 / 文件被占用…统一兜底：留副本 + 用默认值。
        # 这里故意接宽一点：load 的承诺是「绝不抛」，不然 AI 页会打不开。
        _backup_bad_file(path)
        return data

    if not isinstance(raw, Mapping):
        _backup_bad_file(path)
        return data

    merged = _deep_merge(DEFAULT_SETTINGS, raw)

    llm = merged.get("llm")
    if not isinstance(llm, dict):
        llm = copy.deepcopy(DEFAULT_SETTINGS["llm"])
        merged["llm"] = llm
    host = merged.get("host")
    if not isinstance(host, dict):
        merged["host"] = copy.deepcopy(DEFAULT_SETTINGS["host"])

    encrypted = llm.get("api_key_enc")
    if isinstance(encrypted, str) and encrypted:
        decrypted = dpapi_unprotect(encrypted)
        # 解密失败但文件里还留着明文（手写/老版本）→ 保留明文
        if decrypted or not llm.get("api_key"):
            llm["api_key"] = decrypted
    if not isinstance(llm.get("api_key"), str):
        llm["api_key"] = ""
    # 回给上层的设置不带密文，避免 UI/日志顺手把 blob 显示出来
    llm.pop("api_key_enc", None)
    return merged


def save_ai(data: dict) -> tuple[bool, str]:
    """保存设置。返回 ``(ok, 说明)``，成功时说明为空串。

    写盘前把 ``llm.api_key`` 加密成 ``llm.api_key_enc`` 并删掉明文字段；
    调用方传 ``load_ai()`` 的结果回来即可，不传全也没关系（缺省深合并）。
    """
    if not isinstance(data, Mapping):
        return False, "AI 设置必须是一个 dict"

    tmp = ""
    try:
        payload = _deep_merge(DEFAULT_SETTINGS, data)

        llm = payload.get("llm")
        if not isinstance(llm, dict):
            llm = copy.deepcopy(DEFAULT_SETTINGS["llm"])
            payload["llm"] = llm
        host = payload.get("host")
        if not isinstance(host, dict):
            payload["host"] = copy.deepcopy(DEFAULT_SETTINGS["host"])

        key = llm.get("api_key")
        key = key.strip() if isinstance(key, str) else ""
        old_encrypted = llm.get("api_key_enc")
        old_encrypted = old_encrypted if isinstance(old_encrypted, str) else ""

        llm.pop("api_key", None)
        if key:
            llm["api_key_enc"] = dpapi_protect(key)
        elif old_encrypted:
            # 调用方直接拿着原始配置（含密文）来保存：保留原密文
            llm["api_key_enc"] = old_encrypted
        else:
            llm.pop("api_key_enc", None)

        path = ai_path()
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True, ""
    except Exception as exc:
        # 清掉可能的残留临时文件，免得下次误读
        try:
            if tmp and os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, f"AI 设置保存失败：{type(exc).__name__}: {exc}"
