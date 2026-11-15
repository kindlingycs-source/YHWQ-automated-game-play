# -*- coding: utf-8 -*-
"""ai_store 单测：默认值 / 深合并 / DPAPI / 明文不落盘 / 坏文件兜底。

每个用例都把 ``FOXAI_HOME`` 指到临时目录，真实项目里的 ai.json
一个字节都不会动。
"""

import json
import os
import re
import shutil
import sys
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_store  # noqa: E402


class _StoreTest(unittest.TestCase):
    """公共夹具：临时 FOXAI_HOME + 用完删干净。"""

    def setUp(self):
        # 临时目录放在项目目录里：DSH 沙箱只允许写工作区，
        # 系统 %TEMP% 在沙箱下会 PermissionError。
        # 注意不能用 tempfile.mkdtemp：Python 3.14 在 Windows 上会给它
        # 建一个进程默认 ACL 的目录，沙箱令牌反而写不进去；os.makedirs
        # 继承父目录 ACL，才能正常读写。
        base = os.path.join(ROOT, "_test_tmp")
        os.makedirs(base, exist_ok=True)
        self.tmp = os.path.join(base, "foxai-store-" + uuid.uuid4().hex[:12])
        os.makedirs(self.tmp)

        def cleanup():
            shutil.rmtree(self.tmp, ignore_errors=True)
            try:
                os.rmdir(base)          # 最后一个用例跑完把底座也删掉
            except OSError:
                pass

        self.addCleanup(cleanup)
        patcher = mock.patch.dict(os.environ, {"FOXAI_HOME": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, data) -> str:
        path = ai_store.ai_path()
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f, ensure_ascii=False)
        return path

    def _read_disk(self) -> dict:
        with open(ai_store.ai_path(), encoding="utf-8") as f:
            return json.load(f)

    def _listdir(self) -> list[str]:
        return os.listdir(self.tmp)


class DefaultsTest(_StoreTest):
    """DEFAULT_SETTINGS 必须和契约逐字一致。"""

    def test_default_settings_match_contract(self):
        self.assertEqual(ai_store.DEFAULT_SETTINGS["llm"], {
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": "",
            "model": "doubao-seed-1-6-vision-250815",
            "vision": True,
        })
        self.assertEqual(ai_store.DEFAULT_SETTINGS["host"], {
            "accept_invites": True,
            "chat_auto": True,
            "bet_auto": False,
            "bet_max_single": 10000,
            "bet_max_daily": 50000,
            "bet_min_edge": 0.0,
            "allow_ranked": False,
            "chat_style": "自然、礼貌、偶尔幽默",
        })

    def test_missing_file_returns_defaults(self):
        self.assertEqual(ai_store.load_ai(), ai_store.DEFAULT_SETTINGS)

    def test_load_returns_independent_copies(self):
        first = ai_store.load_ai()
        second = ai_store.load_ai()
        first["llm"]["api_key"] = "改一下"
        first["host"]["bet_max_single"] = 1
        self.assertEqual(second["llm"]["api_key"], "")
        self.assertEqual(second["host"]["bet_max_single"], 10000)
        self.assertEqual(ai_store.DEFAULT_SETTINGS["llm"]["api_key"], "")

    def test_partial_file_deep_merges(self):
        self._write({"llm": {"model": "custom-model"},
                     "host": {"bet_max_single": 500}})
        data = ai_store.load_ai()
        self.assertEqual(data["llm"]["model"], "custom-model")
        self.assertEqual(data["llm"]["base_url"],
                         "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(data["host"]["bet_max_single"], 500)
        self.assertEqual(data["host"]["bet_max_daily"], 50000)
        self.assertTrue(data["host"]["accept_invites"])

    def test_unknown_keys_are_preserved(self):
        self._write({"llm": {"temperature": 0.9}, "extra": {"foo": 1}})
        data = ai_store.load_ai()
        self.assertEqual(data["llm"]["temperature"], 0.9)
        self.assertEqual(data["extra"], {"foo": 1})

    def test_null_llm_falls_back_to_defaults(self):
        self._write({"llm": None, "host": None})
        data = ai_store.load_ai()
        self.assertEqual(data["llm"], ai_store.DEFAULT_SETTINGS["llm"])
        self.assertEqual(data["host"], ai_store.DEFAULT_SETTINGS["host"])


class PathTest(_StoreTest):
    def test_foxai_home_override_and_creation(self):
        nested = os.path.join(self.tmp, "sub", "dir")
        with mock.patch.dict(os.environ, {"FOXAI_HOME": nested}):
            home = ai_store.ai_home()
            self.assertEqual(os.path.abspath(home), os.path.abspath(nested))
            self.assertTrue(os.path.isdir(nested))
            self.assertEqual(ai_store.ai_path(),
                             os.path.join(nested, "ai.json"))

    def test_save_and_load_stay_inside_foxai_home(self):
        ok, note = ai_store.save_ai({"llm": {"api_key": "sk-isolated"}})
        self.assertTrue(ok, note)
        self.assertTrue(ai_store.ai_path().startswith(self.tmp))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "ai.json")))
        self.assertEqual(ai_store.load_ai()["llm"]["api_key"], "sk-isolated")


class DPAPITest(_StoreTest):
    def test_empty_unprotect_returns_empty(self):
        self.assertEqual(ai_store.dpapi_unprotect(""), "")

    def test_empty_protect_returns_empty(self):
        self.assertEqual(ai_store.dpapi_protect(""), "")

    @unittest.skipUnless(os.name == "nt", "DPAPI 只在 Windows 上可用")
    def test_dpapi_roundtrip(self):
        secret = "sk-火山方舟-测试-123"
        blob = ai_store.dpapi_protect(secret)
        self.assertTrue(blob.startswith("dpapi:"), blob[:16])
        self.assertEqual(ai_store.dpapi_unprotect(blob), secret)

    def test_plain_fallback_when_dpapi_unavailable(self):
        with mock.patch.object(ai_store, "_dpapi_protect_bytes",
                               return_value=None):
            blob = ai_store.dpapi_protect("sk-plain-fallback")
        self.assertTrue(blob.startswith("plain:"))
        self.assertEqual(ai_store.dpapi_unprotect(blob), "sk-plain-fallback")

    def test_plain_prefix_can_be_read_back(self):
        import base64
        blob = "plain:" + base64.b64encode("hello-中文".encode("utf-8")).decode()
        self.assertEqual(ai_store.dpapi_unprotect(blob), "hello-中文")

    def test_unknown_prefix_returns_empty(self):
        self.assertEqual(ai_store.dpapi_unprotect("rot13:abcd"), "")

    def test_malformed_blob_returns_empty(self):
        self.assertEqual(ai_store.dpapi_unprotect("dpapi:@@not-base64@@"), "")


class SaveTest(_StoreTest):
    def test_save_encrypts_key_and_never_writes_plaintext(self):
        secret = "sk-secret-XYZ-123456"
        ok, note = ai_store.save_ai({"llm": {"api_key": secret}})
        self.assertTrue(ok, note)
        self.assertEqual(note, "")

        with open(ai_store.ai_path(), encoding="utf-8") as f:
            raw_text = f.read()
        self.assertNotIn(secret, raw_text)

        disk = self._read_disk()
        self.assertNotIn("api_key", disk["llm"])
        self.assertIn("api_key_enc", disk["llm"])
        self.assertRegex(disk["llm"]["api_key_enc"], r"^(dpapi|plain):")
        self.assertEqual(ai_store.load_ai()["llm"]["api_key"], secret)

    def test_save_partial_data_merges_defaults(self):
        ok, note = ai_store.save_ai({"host": {"bet_auto": True},
                                     "llm": {"model": "my-model"}})
        self.assertTrue(ok, note)
        disk = self._read_disk()
        self.assertEqual(disk["llm"]["base_url"],
                         "https://ark.cn-beijing.volces.com/api/v3")
        self.assertTrue(disk["host"]["bet_auto"])
        self.assertFalse(disk["host"]["allow_ranked"])
        self.assertEqual(disk["host"]["bet_max_single"], 10000)

    def test_save_uses_atomic_replace_and_leaves_no_tmp(self):
        self.assertTrue(ai_store.save_ai({"llm": {"model": "v1"}})[0])
        self.assertTrue(ai_store.save_ai({"llm": {"model": "v2"}})[0])
        names = self._listdir()
        self.assertIn("ai.json", names)
        self.assertFalse([n for n in names if n.endswith(".tmp")])
        self.assertEqual(ai_store.load_ai()["llm"]["model"], "v2")

    def test_failed_replace_keeps_old_file(self):
        self.assertTrue(ai_store.save_ai({"llm": {"model": "old"}})[0])
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            ok, note = ai_store.save_ai({"llm": {"model": "new"}})
        self.assertFalse(ok)
        self.assertIn("保存失败", note)
        self.assertEqual(ai_store.load_ai()["llm"]["model"], "old")
        self.assertFalse([n for n in self._listdir() if n.endswith(".tmp")])

    def test_save_rejects_non_dict(self):
        ok, note = ai_store.save_ai("不是 dict")
        self.assertFalse(ok)
        self.assertIn("dict", note)
        self.assertFalse(os.path.exists(ai_store.ai_path()))

    def test_empty_key_writes_no_encrypted_field(self):
        ok, note = ai_store.save_ai({"llm": {"api_key": ""}})
        self.assertTrue(ok, note)
        disk = self._read_disk()
        self.assertNotIn("api_key", disk["llm"])
        self.assertNotIn("api_key_enc", disk["llm"])
        self.assertEqual(ai_store.load_ai()["llm"]["api_key"], "")

    def test_existing_encrypted_blob_is_kept(self):
        # 调用方拿着原始配置（只有密文、没有明文）来保存时不能把 key 弄丢
        import base64
        blob = "plain:" + base64.b64encode("kept-key".encode("utf-8")).decode()
        ok, note = ai_store.save_ai({"llm": {"api_key_enc": blob}})
        self.assertTrue(ok, note)
        self.assertEqual(self._read_disk()["llm"]["api_key_enc"], blob)
        self.assertEqual(ai_store.load_ai()["llm"]["api_key"], "kept-key")

    def test_load_with_broken_encrypted_blob_is_empty_not_crash(self):
        self._write({"llm": {"api_key_enc": "dpapi:@@broken@@"}})
        self.assertEqual(ai_store.load_ai()["llm"]["api_key"], "")


class BadFileTest(_StoreTest):
    def _bad_copies(self) -> list[str]:
        return [n for n in self._listdir() if n.startswith("ai.json.bad-")]

    def test_broken_json_backs_up_and_returns_defaults(self):
        self._write("{这不是合法 JSON")
        data = ai_store.load_ai()
        self.assertEqual(data, ai_store.DEFAULT_SETTINGS)

        copies = self._bad_copies()
        self.assertEqual(len(copies), 1)
        self.assertRegex(copies[0], r"^ai\.json\.bad-\d{8}-\d{6}(-\d+)?$")
        with open(os.path.join(self.tmp, copies[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{这不是合法 JSON")

    def test_top_level_list_is_treated_as_broken(self):
        self._write([1, 2, 3])
        self.assertEqual(ai_store.load_ai(), ai_store.DEFAULT_SETTINGS)
        self.assertEqual(len(self._bad_copies()), 1)

    def test_empty_file_is_treated_as_broken(self):
        self._write("")
        self.assertEqual(ai_store.load_ai(), ai_store.DEFAULT_SETTINGS)
        self.assertEqual(len(self._bad_copies()), 1)


if __name__ == "__main__":
    unittest.main()
