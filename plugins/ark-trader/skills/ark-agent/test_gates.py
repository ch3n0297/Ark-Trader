"""閘門模組純邏輯測試"""
import json
import os
import tempfile
import unittest

import gates


class TestDefaultsAndBounds(unittest.TestCase):
    def test_預設值都在區間內(self):
        self.assertEqual(gates.range_violations(gates.DEFAULTS), [])

    def test_每個閘門都有區間(self):
        self.assertEqual(set(gates.DEFAULTS), set(gates.BOUNDS))


class TestRange(unittest.TestCase):
    def test_超出上界被列出(self):
        v = gates.range_violations({**gates.DEFAULTS, "name_cap": 0.16})
        self.assertTrue(any("name_cap" in x for x in v))

    def test_停損寬於下界被列出(self):
        v = gates.range_violations({**gates.DEFAULTS, "stop_loss": -0.16})
        self.assertTrue(any("stop_loss" in x for x in v))

    def test_NaN算違規(self):
        """NaN 的所有比較都是 False，區間檢查會靜默放行——必須另外擋"""
        v = gates.range_violations({**gates.DEFAULTS, "name_cap": float("nan")})
        self.assertTrue(any("name_cap" in x for x in v))

    def test_無限大算違規(self):
        v = gates.range_violations({**gates.DEFAULTS, "sector_cap": float("inf")})
        self.assertTrue(any("sector_cap" in x for x in v))

    def test_未知鍵不算違規也不擋(self):
        self.assertEqual(gates.range_violations({**gates.DEFAULTS, "history": []}), [])


class TestStep(unittest.TestCase):
    def test_一步以內放行(self):
        self.assertEqual(gates.step_violations(gates.DEFAULTS,
                                               {**gates.DEFAULTS, "name_cap": 0.13}), [])

    def test_超過步幅被列出(self):
        v = gates.step_violations(gates.DEFAULTS, {**gates.DEFAULTS, "name_cap": 0.145})
        self.assertTrue(any("name_cap" in x for x in v))

    def test_浮點誤差不算超步(self):
        self.assertEqual(gates.step_violations({"sector_cap": 0.25},
                                               {"sector_cap": 0.275}), [])

    def test_改前非數字不crashes而是跳過該鍵(self):
        # Fix: step_violations 應檢查 before[key] 也是數字
        v = gates.step_violations({"name_cap": "bad"}, {**gates.DEFAULTS, "name_cap": 0.13})
        self.assertEqual(v, [])


class TestLoad(unittest.TestCase):
    def test_檔案不存在回預設(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(gates.load(os.path.join(d, "gates.json")), gates.DEFAULTS)

    def test_檔案值蓋過預設並保留其他鍵(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gates.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"name_cap": 0.10, "history": [{"date": "2026-09-12"}]}, fh)
            g = gates.load(p)
            self.assertEqual(g["name_cap"], 0.10)
            self.assertEqual(g["sector_cap"], 0.25)
            self.assertEqual(g["history"], [{"date": "2026-09-12"}])

    def test_區間外的檔案值直接報錯(self):
        # 靜默夾回區間會讓複盤層以為改成功了
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gates.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"name_cap": 0.30}, fh)
            with self.assertRaises(ValueError):
                gates.load(p)

    def test_檔案頂層非物件直接報ValueError(self):
        # Fix: load() 應檢查頂層是 dict 不是 list
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gates.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([1, 2, 3], fh)
            with self.assertRaises(ValueError):
                gates.load(p)


class TestIntact(unittest.TestCase):
    def test_合法改動通過(self):
        before = json.dumps(gates.DEFAULTS)
        after = json.dumps({**gates.DEFAULTS, "stop_loss": -0.11})
        ok, why = gates.intact(before, after)
        self.assertTrue(ok, why)

    def test_壞掉的_JSON_不通過(self):
        ok, why = gates.intact(json.dumps(gates.DEFAULTS), "{not json")
        self.assertFalse(ok)

    def test_超步或超區間不通過(self):
        before = json.dumps(gates.DEFAULTS)
        ok, why = gates.intact(before, json.dumps({**gates.DEFAULTS, "share_of_invested": 0.7}))
        self.assertFalse(ok)
        self.assertTrue(any("share_of_invested" in x for x in why))

    def test_原本沒有檔案時只驗區間(self):
        ok, _ = gates.intact("", json.dumps({**gates.DEFAULTS, "name_cap": 0.15}))
        self.assertTrue(ok)

    def test_改前非字典時跳過步幅檢查(self):
        # Fix: intact() before branch 應處理非 dict JSON
        before = json.dumps([1, 2, 3])
        after = json.dumps({**gates.DEFAULTS, "name_cap": 0.15})
        ok, why = gates.intact(before, after)
        self.assertTrue(ok, why)

    def test_改前含非數字值時報告違規不crashes(self):
        # Fix: intact() 應檢查 before 中的值也是數字
        before = json.dumps({**gates.DEFAULTS, "name_cap": "bad"})
        after = json.dumps({**gates.DEFAULTS, "name_cap": 0.13})
        ok, why = gates.intact(before, after)
        self.assertTrue(ok, why)


class TestDefaultPath(unittest.TestCase):
    def test_跟著準則檔目錄走(self):
        old = dict(os.environ)
        try:
            os.environ.pop("ARK_GATES", None)
            os.environ["ARK_RULES"] = "/x/docs/decision-rules.md"
            self.assertEqual(gates.default_path(), "/x/docs/gates.json")
            os.environ["ARK_GATES"] = "/y/gates.json"
            self.assertEqual(gates.default_path(), "/y/gates.json")
        finally:
            os.environ.clear()
            os.environ.update(old)
