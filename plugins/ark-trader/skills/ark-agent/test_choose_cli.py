"""choose_cli 純邏輯測試（不跳視窗；任何平台可跑）"""
import unittest

import choose_cli as cc


class TestCurrent(unittest.TestCase):
    def test_未設定時回None(self):
        self.assertIsNone(cc.current_cli("ARK_BACKEND=live\n"))

    def test_讀得到含export與引號(self):
        self.assertEqual(cc.current_cli('export ARK_DECISION_CLI="codex"\n'), "codex")

    def test_註解掉的不算(self):
        self.assertIsNone(cc.current_cli("# ARK_DECISION_CLI=codex\n"))


class TestUpdateEnv(unittest.TestCase):
    def test_空檔寫入兩行(self):
        out = cc.update_env("", "codex", "gpt-6-astra")
        self.assertIn("ARK_DECISION_CLI=codex", out.splitlines())
        self.assertIn("ARK_DECISION_MODEL=gpt-6-astra", out.splitlines())

    def test_取代舊值_保留其他設定與註解(self):
        src = "# 個人設定\nARK_RULES=/x/rules.md\nARK_DECISION_MODEL=fable\nARK_BACKEND=live\n"
        lines = cc.update_env(src, "codex", "gpt-6-astra").splitlines()
        self.assertEqual(lines[:2], ["# 個人設定", "ARK_RULES=/x/rules.md"])
        self.assertIn("ARK_BACKEND=live", lines)
        self.assertNotIn("ARK_DECISION_MODEL=fable", lines)
        self.assertEqual(sum(l.startswith("ARK_DECISION_MODEL=") for l in lines), 1)

    def test_模型為None時移除模型行(self):
        out = cc.update_env("ARK_DECISION_MODEL=fable\n", "codex", None)
        self.assertNotIn("ARK_DECISION_MODEL", out)
        self.assertIn("ARK_DECISION_CLI=codex", out)

    def test_export形式也會被取代(self):
        out = cc.update_env("export ARK_DECISION_CLI=claude\n", "codex", None)
        self.assertEqual(out.count("ARK_DECISION_CLI"), 1)

    def test_結尾有換行(self):
        self.assertTrue(cc.update_env("A=1", "claude", "fable").endswith("\n"))


class TestChoices(unittest.TestCase):
    def test_只列出裝了的CLI(self):
        self.assertEqual(cc.available_clis(lambda n: n == "codex"), ["codex"])
        self.assertEqual(cc.available_clis(lambda n: True), ["claude", "codex"])

    def test_每支CLI的第一個模型是預設(self):
        self.assertEqual(cc.MODELS["claude"][0], "fable")
        self.assertEqual(cc.MODELS["codex"][0], "gpt-6-astra")

    def test_Codex預設選項對應None(self):
        self.assertIsNone(cc.model_value(cc.CODEX_DEFAULT))
        self.assertEqual(cc.model_value("opus"), "opus")


class TestDialog(unittest.TestCase):
    def test_取消回None(self):
        self.assertIsNone(cc.parse_choice("false\n"))

    def test_選擇回字串(self):
        self.assertEqual(cc.parse_choice("codex\n"), "codex")

    def test_腳本含選項與預設且引號跳脫(self):
        script = cc.list_script("選決策層", ["claude", 'co"dex'], "claude")
        self.assertIn("choose from list", script)
        self.assertIn('default items {"claude"}', script)
        self.assertIn('co\\"dex', script)


class TestRun(unittest.TestCase):
    def run_with(self, env_text, picks, if_missing=False):
        it = iter(picks)
        return cc.run(env_text, ask=lambda title, items, default: next(it),
                      which=lambda n: True, if_missing=if_missing)

    def test_初次安裝_選codex與astra(self):
        out = self.run_with("", ["codex", "gpt-6-astra"])
        self.assertIn("ARK_DECISION_CLI=codex", out)
        self.assertIn("ARK_DECISION_MODEL=gpt-6-astra", out)

    def test_取消CLI就不改(self):
        self.assertIsNone(self.run_with("A=1\n", [None]))

    def test_取消模型也不改(self):
        self.assertIsNone(self.run_with("A=1\n", ["claude", None]))

    def test_if_missing_已設定就不跳視窗(self):
        def boom(*a):
            raise AssertionError("不該跳視窗")
        self.assertIsNone(cc.run("ARK_DECISION_CLI=claude\n", ask=boom,
                                 which=lambda n: True, if_missing=True))

    def test_兩支CLI都沒裝就不跳視窗(self):
        def boom(*a):
            raise AssertionError("不該跳視窗")
        self.assertIsNone(cc.run("", ask=boom, which=lambda n: False))


if __name__ == "__main__":
    unittest.main()
