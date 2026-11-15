# -*- coding: utf-8 -*-
"""内置自动更新 —— 检查 Gitee 仓库上的 version.json，下载新 exe 并替换。

为什么这么设计（2026-11-02 定）
------------------------------------------------------------
· 仓库公开、不需要任何 token：检查走 raw 地址，下载走 raw 地址。
· version.json 放仓库根目录，字段：
      {"version": "3.1.4",
       "url":     "https://gitee.com/.../raw/master/野狐自动对弈.exe",
       "md5":     "....",
       "notes":   "这一版改了什么（可选）"}
· 新版 exe 先下到 %LOCALAPPDATA%\\野狐自动对弈\\update\\，**校验 MD5**
  通过后才动原文件；校验不过直接放弃，绝不用半个文件覆盖能用的 exe。
· 替换用 update.bat：等旧进程退出 → copy 覆盖 → 启动新 exe → 自删。
  Windows 不允许覆盖正在运行的 exe，所以必须由一个「旁观者」来干这活。
· 只依赖标准库 urllib，PyInstaller 不用额外 hiddenimport。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

REPO = "kindlingycs/wildfox-automated-go-matches"
GITEE_RAW = "https://gitee.com/%s/raw/master" % REPO
VERSION_URL = GITEE_RAW + "/version.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FoxAuto-Updater"


def current_exe():
    """当前正在运行的 exe（源码运行时返回 None —— 不自动替换）"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return None


def _vtuple(v):
    out = []
    for p in str(v or "").replace("v", "").split("."):
        try:
            out.append(int(p))
        except Exception:
            out.append(0)
    out = (out + [0, 0, 0])[:3]
    return tuple(out)


def _fetch_json(url, timeout=10):
    req = urllib.request.Request(
        url + ("&" if "?" in url else "?") + "t=%d" % int(time.time()),
        headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def check(current_version, timeout=10):
    """有新版返回 info dict；没有 / 网络失败返回 None（静默）"""
    try:
        info = _fetch_json(VERSION_URL, timeout)
    except Exception:
        return None
    ver = str(info.get("version") or "").strip().lstrip("v")
    url = str(info.get("url") or "").strip()
    if not ver or not url:
        return None
    if _vtuple(ver) <= _vtuple(current_version):
        return None
    info["version"] = ver
    info["url"] = url if url.startswith("http") else GITEE_RAW + "/" + urllib.parse.quote(url)
    return info


def download(info, dest_dir, progress=None):
    """下载新版（zip 或 exe）到 dest_dir，返回**解出来的 exe 路径**。

    2026-11-02：仓库改成只放一个全打包 zip，所以这里要能
      ① 下载 zip → 校验整个 zip 的 MD5 → 从里面解出 info["asset"] 的 exe
      ② 兼容直接给 exe 链接的老格式
    """
    import shutil
    import zipfile

    os.makedirs(dest_dir, exist_ok=True)
    # ⚠️ 中文文件名：urllib 不接受非 ASCII URL，必须先 percent-encode
    url = info["url"]
    if any(ord(c) > 127 for c in url):
        url = urllib.parse.quote(url, safe=":/?&=#%")
    is_zip = url.lower().split("?")[0].endswith(".zip")
    dl = os.path.join(dest_dir, "download" + (".zip" if is_zip else ".exe"))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.md5()
    with urllib.request.urlopen(req, timeout=180) as r, open(dl, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress:
                try:
                    progress(got, total)
                except Exception:
                    pass
    want = (info.get("md5") or "").strip().lower()
    if want and h.hexdigest().lower() != want:
        try:
            os.remove(dl)
        except Exception:
            pass
        raise RuntimeError("下载文件 MD5 不对（%s ≠ %s），已丢弃"
                           % (h.hexdigest()[:12], want[:12]))
    if not is_zip:
        return dl

    # ---- 从 zip 里解出 exe ----
    asset = (info.get("asset") or "野狐自动对弈.exe").lstrip("/").replace("\\", "/")
    out = None
    try:
        with zipfile.ZipFile(dl) as z:
            names = z.namelist()
            target = None
            for n in names:
                if os.path.basename(n) == os.path.basename(asset):
                    target = n
                    break
            if target is None:
                for n in names:
                    if n.lower().endswith(".exe"):
                        target = n
                        break
            if target is None:
                raise RuntimeError("zip 里没找到 exe：%s" % asset)
            out = os.path.join(dest_dir, "new-" + os.path.basename(target))
            with z.open(target) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
    finally:
        try:
            os.remove(dl)
        except Exception:
            pass
    return out


def _write_bat(bat_path, new_exe, target_exe):
    """GBK 写 bat（cmd 默认按系统代码页读，UTF-8 会乱码）"""
    txt = (
        "@echo off\r\n"
        "chcp 936 >nul\r\n"
        ":wait\r\n"
        'tasklist /FI "IMAGENAME eq %(img)s" 2>nul | find /I "%(img)s" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  ping 127.0.0.1 -n 2 >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        'copy /y "%(new)s" "%(dst)s" >nul\r\n'
        'if errorlevel 1 (\r\n'
        '  echo update failed > "%(log)s"\r\n'
        "  exit /b 1\r\n"
        ")\r\n"
        'start "" "%(dst)s"\r\n'
        'del "%%~f0"\r\n'
    ) % {
        "img": os.path.basename(target_exe),
        "new": new_exe,
        "dst": target_exe,
        "log": os.path.join(os.path.dirname(bat_path), "update-failed.txt"),
    }
    with open(bat_path, "w", encoding="gbk", errors="replace", newline="") as f:
        f.write(txt)


def install_now(new_exe, target_exe, log_dir=None):
    """写 update.bat 并启动；调用方应随即退出进程（os._exit(0)）"""
    base = log_dir or os.path.dirname(new_exe)
    bat = os.path.join(base, "update.bat")
    _write_bat(bat, new_exe, target_exe)
    # DETACHED：不跟着本进程一起死
    subprocess.Popen(["cmd.exe", "/c", bat],
                     cwd=base,
                     creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_GROUP
                     close_fds=True)
    return bat
