"""choose_auth 純邏輯測試（不跳視窗、不碰 Keychain、不呼叫 claude；任何平台可跑）"""
import unittest

import choose_auth as ca


class TestCurrent(unittest.TestCase):
    def test_沒有這行回None_代表還沒選過(self):
        self.assertIsNone(ca.current_auth("ARK_BACKEND=live\n"))

    def test_空值代表選了訂閱(self):
        self.assertEqual(ca.current_auth("ARK_ANTHROPIC_KEYCHAIN=\n"), "")

    def test_讀得到服務名_含export與引號(self):
        self.assertEqual(ca.current_auth('export ARK_ANTHROPIC_KEYCHAIN="svc"\n'), "svc")

    def test_註解掉的不算(self):
        self.assertIsNone(ca.current_auth("# ARK_ANTHROPIC_KEYCHAIN=svc\n"))


class TestUpdateEnv(unittest.TestCase):
    def test_取代舊值_保留其他行(self):
        src = "# 註解\nARK_RULES=/x\nARK_ANTHROPIC_KEYCHAIN=old\nARK_BACKEND=live\n"
        lines = ca.update_env(src, "svc").splitlines()
        self.assertEqual(lines[:2], ["# 註解", "ARK_RULES=/x"])
        self.assertIn("ARK_BACKEND=live", lines)
        self.assertEqual([l for l in lines if "ARK_ANTHROPIC_KEYCHAIN" in l],
                         ["ARK_ANTHROPIC_KEYCHAIN=svc"])

    def test_訂閱寫成空值(self):
        self.assertIn("ARK_ANTHROPIC_KEYCHAIN=\n", ca.update_env("", ""))

    def test_結尾有換行(self):
        self.assertTrue(ca.update_env("A=1", "svc").endswith("\n"))


class TestVerify(unittest.TestCase):
    def test_驗證用乾淨環境_不繼承呼叫端的變數(self):
        env = ca.verify_env("sk-test", home="/h", path="/p")
        self.assertEqual(env, {"HOME": "/h", "PATH": "/p", "ANTHROPIC_API_KEY": "sk-test"})

    def test_驗證指令與排程同樣隔離_用便宜模型(self):
        cmd = ca.verify_command()
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        for flag in ("--setting-sources", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")

    def test_成功(self):
        ok, msg = ca.parse_verify(0, '{"is_error": false, "result": "OK", "total_cost_usd": 0.001}')
        self.assertTrue(ok)

    def test_額度不足或Key無效都算失敗並帶原因(self):
        ok, msg = ca.parse_verify(1, '{"is_error": true, "result": "Credit balance is too low"}')
        self.assertFalse(ok)
        self.assertIn("Credit balance is too low", msg)

    def test_輸出不是JSON也算失敗(self):
        ok, msg = ca.parse_verify(1, "boom")
        self.assertFalse(ok)
        self.assertIn("boom", msg)


class TestRun(unittest.TestCase):
    def run_with(self, text, picks, has_item=False, verify_ok=True, if_missing=False,
                 which=lambda n: True):
        it = iter(picks)
        self.stored = []
        return ca.run(text,
                      ask=lambda prompt, items, default: next(it),
                      which=which,
                      has_item=lambda: has_item,
                      store_key=lambda: self.stored.append(1) or True,
                      verify=lambda: (verify_ok, "原因"),
                      if_missing=if_missing)

    def test_選訂閱_寫空值_不碰Keychain(self):
        new, _ = self.run_with("", [ca.SUBSCRIPTION])
        self.assertIn("ARK_ANTHROPIC_KEYCHAIN=\n", new)
        self.assertEqual(self.stored, [])

    def test_選API_Key_沒有舊Key就輸入_驗證通過才寫(self):
        new, _ = self.run_with("", [ca.API_KEY])
        self.assertIn(f"ARK_ANTHROPIC_KEYCHAIN={ca.SERVICE}\n", new)
        self.assertEqual(self.stored, [1])

    def test_已有Key可沿用_不重新輸入(self):
        new, _ = self.run_with("", [ca.API_KEY, ca.REUSE], has_item=True)
        self.assertIn(f"ARK_ANTHROPIC_KEYCHAIN={ca.SERVICE}", new)
        self.assertEqual(self.stored, [])

    def test_已有Key選重新輸入(self):
        self.run_with("", [ca.API_KEY, ca.REPLACE], has_item=True)
        self.assertEqual(self.stored, [1])

    def test_驗證失敗就不改檔並回報原因(self):
        new, msg = self.run_with("A=1\n", [ca.API_KEY], verify_ok=False)
        self.assertIsNone(new)
        self.assertIn("原因", msg)

    def test_取消就不改(self):
        new, _ = self.run_with("A=1\n", [None])
        self.assertIsNone(new)

    def test_if_missing_選過就不跳視窗(self):
        def boom(*a):
            raise AssertionError("不該跳視窗")
        new, _ = ca.run("ARK_ANTHROPIC_KEYCHAIN=\n", ask=boom, which=lambda n: True,
                        has_item=boom, store_key=boom, verify=boom, if_missing=True)
        self.assertIsNone(new)

    def test_沒裝claude就不問(self):
        def boom(*a):
            raise AssertionError("不該跳視窗")
        new, _ = ca.run("", ask=boom, which=lambda n: False, has_item=boom,
                        store_key=boom, verify=boom)
        self.assertIsNone(new)


if __name__ == "__main__":
    unittest.main()
