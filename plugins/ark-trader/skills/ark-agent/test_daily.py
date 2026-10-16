"""每日編排的純邏輯測試（不需要 ARK、Shioaji 或 launchd，任何平台可跑）"""
import json
import os
import tempfile
import unittest
from unittest import mock

import daily


def setUpModule():
    """整個模組的 daily.STATE／LOG 指到暫存目錄：測試絕不寫進使用者的 ~/.ark-toolkit/agent。"""
    global _state_dir, _patches
    _state_dir = tempfile.TemporaryDirectory()
    _patches = [mock.patch.object(daily, "STATE", _state_dir.name),
                mock.patch.object(daily, "LOG", os.path.join(_state_dir.name, "daily.log"))]
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()
    _state_dir.cleanup()


class TestIsTradingWeekday(unittest.TestCase):
    def test_平日為真(self):
        for d in ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
                  "2026-08-14"):
            self.assertTrue(daily.is_trading_weekday(d), d)

    def test_週末為假(self):
        self.assertFalse(daily.is_trading_weekday("2026-08-15"))   # 六
        self.assertFalse(daily.is_trading_weekday("2026-08-16"))   # 日


class TestDecisionWindow(unittest.TestCase):
    """launchd 對錯過的 StartCalendarInterval 會在喚醒後補跑一次。10:00 的判斷
    在 13:00 執行是另一回事——盤中價格早已不同，限價也全部失效。時間窗必須由
    程式自己守，不能寄望排程器的語意。"""

    def test_排定時間附近放行(self):
        for t in ("09:55", "10:00", "10:20", "10:45"):
            self.assertTrue(daily.is_within_window(t, "10:00", 60), t)

    def test_超出容許範圍拒絕(self):
        for t in ("08:30", "11:05", "13:20", "23:00"):
            self.assertFalse(daily.is_within_window(t, "10:00", 60), t)

    def test_排定時間之前也擋(self):
        """機器提早喚醒時，09:00 就跑會拿到還沒重算過的 ARK 指標"""
        self.assertFalse(daily.is_within_window("08:59", "10:00", 60))

    def test_邊界含端點(self):
        self.assertTrue(daily.is_within_window("09:00", "10:00", 60))
        self.assertTrue(daily.is_within_window("11:00", "10:00", 60))

    def test_結算窗較寬鬆(self):
        """結算是對既成事實的記錄，晚幾小時做結果一樣，窗開大一點無妨"""
        self.assertTrue(daily.is_within_window("17:00", "14:30", 240))


class TestRiskExitCode(unittest.TestCase):
    """risk.py 用離開碼區分「今天不該交易」（3）與「產不出邊界」（2）。
    編排把兩者混為一談的話，全面熔斷會被誤報成系統故障。"""

    def test_零為正常繼續(self):
        self.assertEqual(daily.classify_risk_exit(0), daily.PROCEED)

    def test_三為今日不交易(self):
        self.assertEqual(daily.classify_risk_exit(3), daily.SKIP)

    def test_其餘為錯誤(self):
        for code in (1, 2, 127, -9):
            self.assertEqual(daily.classify_risk_exit(code), daily.FAIL, code)


class TestPromptRendering(unittest.TestCase):
    TEMPLATE = ("讀 PACKET_PATH 與 ENVELOPE_PATH，"
                "寫到 DECISION_PATH，日期 TODAY。")

    def test_四個佔位符都被換掉(self):
        out = daily.render_prompt(self.TEMPLATE, packet="/p.json",
                                  envelope="/e.json", decision="/d.json",
                                  date="2026-08-11")
        self.assertEqual(out, "讀 /p.json 與 /e.json，寫到 /d.json，日期 2026-08-11。")

    def test_未被取代的佔位符會報錯(self):
        """漏換 DECISION_PATH 的話 Agent 會把決策寫到字面上的 'DECISION_PATH'，
        排程接著找不到檔案——與其那樣，不如當場失敗"""
        with self.assertRaises(ValueError):
            daily.render_prompt("讀 PACKET_PATH 和 MYSTERY_PATH", packet="/p",
                                envelope="/e", decision="/d", date="2026-08-11")


class TestQuotaExhausted(unittest.TestCase):
    """決策模型撞到用量上限時，claude CLI 也是非零離開——與 prompt 寫錯、
    網路斷線混在一起的話，換模型重試就會在不該重試的時候白燒一次決策。"""

    LIMIT = ("You've reached your Fable 5 limit. Run /usage-credits to "
             "continue or switch models with /model.")

    def test_額度訊息為真(self):
        self.assertTrue(daily.is_quota_exhausted(self.LIMIT))

    def test_額度訊息夾在其他輸出中也認得(self):
        """實際 log 裡前面還有工具呼叫與警告，額度訊息在最後一行"""
        self.assertTrue(daily.is_quota_exhausted(
            f"讀取 packet…\nDeprecationWarning: ...\n{self.LIMIT}\n"))

    def test_其他失敗為假(self):
        for out in ("", "Error: connection refused",
                    "提示模板有未取代的佔位符：['MYSTERY_PATH']",
                    "✅ 決策已寫入 /Users/someone/.ark-toolkit/agent/decisions/x.json"):
            self.assertFalse(daily.is_quota_exhausted(out), out)


class TestFallbackModel(unittest.TestCase):
    """額度耗盡就整天不交易的話，實驗數據會出現非市場因素的缺口——
    但降級只在額度這一種失敗上成立，其餘照舊直接失敗。"""

    LIMIT = "You've reached your Fable 5 limit. Run /usage-credits to continue."

    def test_額度耗盡回傳接手模型(self):
        self.assertEqual(
            daily.fallback_model(1, self.LIMIT, "fable", "opus"), "opus")

    def test_成功時不降級(self):
        """離開碼 0 代表決策已產出，輸出裡出現什麼字樣都不該再跑一次"""
        self.assertIsNone(daily.fallback_model(0, self.LIMIT, "fable", "opus"))

    def test_非額度失敗不降級(self):
        self.assertIsNone(
            daily.fallback_model(1, "Error: connection refused", "fable", "opus"))

    def test_接手模型與現用相同時不降級(self):
        """否則會拿同一個已耗盡的額度池再撞一次牆"""
        self.assertIsNone(
            daily.fallback_model(1, self.LIMIT, "opus", "opus"))

    def test_未設定接手模型時不降級(self):
        for fallback in ("", None):
            self.assertIsNone(
                daily.fallback_model(1, self.LIMIT, "fable", fallback), fallback)


class TestSettleOutcome(unittest.TestCase):
    """結算的每一步都要盡力做完再回報——成交對回失敗不該讓淨值記錄也跳過，
    否則熔斷基準會斷掉。"""

    def test_全成功(self):
        self.assertEqual(daily.settle_failures([("對回", 0), ("淨值", 0)]), [])

    def test_只回報失敗的步驟(self):
        self.assertEqual(
            daily.settle_failures([("對回", 1), ("淨值", 0), ("同步", 2)]),
            ["對回", "同步"])


class TestUnattendedSync(unittest.TestCase):
    def test_排程呼叫_sync_一定帶_no_prompt(self):
        """launchd 跑結算時沒人按視窗：config 缺均價口徑若開視窗詢問，整條流程會卡死"""
        self.assertIn("--no-prompt", daily.SYNC_ARGS)
        self.assertIn("--allow-delete", daily.SYNC_ARGS)
        self.assertIn("--with-cash", daily.SYNC_ARGS)


if __name__ == "__main__":
    unittest.main()


class TestReviewWindow(unittest.TestCase):
    def test_複盤窗在盤後且寬鬆(self):
        """複盤是對既成事實的檢討，晚幾小時做結果一樣；但不該在盤中跑"""
        scheduled, tolerance = daily.WINDOWS["review"]
        self.assertTrue(daily.is_within_window("15:00", scheduled, tolerance))
        self.assertTrue(daily.is_within_window("18:00", scheduled, tolerance))
        self.assertFalse(daily.is_within_window("10:00", scheduled, tolerance))


class TestReviewPromptRendering(unittest.TestCase):
    TEMPLATE = "讀 REVIEW_PATH 與 RULES_PATH，寫到 REVIEW_DOC_PATH，日期 TODAY。"

    def test_四個佔位符都被換掉(self):
        out = daily.render_review_prompt(self.TEMPLATE, review="/r.txt",
                                         rules="/rules.md", doc="/doc.md",
                                         date="2026-09-04")
        self.assertEqual(out, "讀 /r.txt 與 /rules.md，寫到 /doc.md，日期 2026-09-04。")

    def test_未被取代的佔位符會報錯(self):
        with self.assertRaises(ValueError):
            daily.render_review_prompt("讀 REVIEW_PATH 和 MYSTERY_PATH", review="/r",
                                       rules="/x", doc="/d", date="2026-09-04")

    def test_複盤提示帶閘門檔路徑(self):
        out = daily.render_review_prompt("R=REVIEW_PATH G=GATES_PATH D=TODAY",
                                         "/r.txt", "/rules.md", "/doc.md", "2026-09-12",
                                         gates_path="/docs/gates.json")
        self.assertIn("G=/docs/gates.json", out)


class TestReviewDocPath(unittest.TestCase):
    def test_檢討記錄放在準則檔旁的decision_reviews(self):
        self.assertEqual(
            daily.review_doc_path("/proj/docs/decision-rules.md", "2026-09-04"),
            "/proj/docs/decision-reviews/2026-09-04.md")


class TestRulesIntact(unittest.TestCase):
    """複盤由模型改準則檔。改壞了（編號消失、標題格式解析不到）隔天決策就拿不到
    準則，而且不會有人發現——寫回前要驗，驗不過就從備份還原。"""
    BEFORE = "### R-001 甲\n內文\n### R-002 乙\n內文\n"

    def test_原樣或新增準則都算完整(self):
        self.assertTrue(daily.rules_intact(self.BEFORE, self.BEFORE))
        self.assertTrue(daily.rules_intact(self.BEFORE,
                                           self.BEFORE + "### R-003 丙\n內文\n"))

    def test_淘汰的準則移到檔末仍算完整(self):
        moved = "### R-002 乙\n內文\n## 已淘汰\n### R-001 甲\n內文\n"
        self.assertTrue(daily.rules_intact(self.BEFORE, moved))

    def test_編號消失算損壞(self):
        self.assertFalse(daily.rules_intact(self.BEFORE, "### R-002 乙\n內文\n"))

    def test_清空算損壞(self):
        self.assertFalse(daily.rules_intact(self.BEFORE, ""))

    def test_標題格式被改壞算損壞(self):
        broken = self.BEFORE.replace("### R-001", "#### R-001")
        self.assertFalse(daily.rules_intact(self.BEFORE, broken))

    def test_原本沒有準則時新增即完整(self):
        self.assertTrue(daily.rules_intact("", "### R-001 甲\n內文\n"))


class TestGatesGuard(unittest.TestCase):
    def test_複盤改壞閘門就還原(self):
        import gates
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gates.json")
            before = json.dumps(gates.DEFAULTS)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({**gates.DEFAULTS, "name_cap": 0.30}))
            restored = daily.guard_gates(p, before, mode="test")
            self.assertTrue(restored)
            with open(p, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), gates.DEFAULTS)

    def test_合法改動不動它(self):
        import gates
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gates.json")
            before = json.dumps(gates.DEFAULTS)
            after = {**gates.DEFAULTS, "name_cap": 0.13}
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(after))
            self.assertFalse(daily.guard_gates(p, before, mode="test"))
            with open(p, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), after)


class TestLogIsolation(unittest.TestCase):
    """測試絕不能寫進使用者的真實 ~/.ark-toolkit/agent/daily.log（自 2026-09-09 起實際外漏 42 行）"""

    def test_日誌不落在使用者目錄(self):
        self.assertFalse(daily.LOG.startswith(os.path.expanduser("~/.ark-toolkit")),
                         f"daily.LOG 仍指向使用者目錄：{daily.LOG}")

    def test_還原理由寫進日誌(self):
        import gates
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gates.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({**gates.DEFAULTS, "name_cap": 0.30}))
            daily.guard_gates(p, json.dumps(gates.DEFAULTS), mode="test")
        with open(daily.LOG, encoding="utf-8") as fh:
            self.assertIn("[test] ⚠️ gates.json 被改到區間或步幅之外，已還原：name_cap", fh.read())


class TestDecidePromptMentionsSatelliteBucket(unittest.TestCase):
    def test_提示含個股桶三道閘門與候選來源(self):
        with open(os.path.join(os.path.dirname(daily.__file__), "prompts", "decide.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        for needle in ("industry.candidates", "name_cap_value", "sector_cap_value",
                       "trim_candidates", "heating"):
            self.assertIn(needle, text)


class TestTradeWindow(unittest.TestCase):
    """手動一場（trade）不看排定時刻，只看是否還來得及在收盤前等單＋結算"""

    def test_交易窗含端點(self):
        for now in ("09:00", "10:43", "13:00"):
            self.assertTrue(daily.is_within_range(now, "09:00", "13:00"), now)

    def test_交易窗之外拒絕(self):
        for now in ("08:59", "13:01", "21:37"):
            self.assertFalse(daily.is_within_range(now, "09:00", "13:00"), now)

    def test_trade為合法模式(self):
        self.assertIn("trade", daily.MODES)


class TestHasDecision(unittest.TestCase):
    D = "2026-09-22"

    def test_當日有決策為真(self):
        self.assertTrue(daily.has_decision([{"type": "decision", "date": self.D}], self.D))

    def test_只有成交對回不算(self):
        self.assertFalse(daily.has_decision(
            [{"type": "fill", "date": self.D, "decision_ref": "2026-09-21"}], self.D))

    def test_別天的決策不算(self):
        self.assertFalse(daily.has_decision([{"type": "decision", "date": "2026-09-21"}], self.D))


class TestTradeOrchestration(unittest.TestCase):
    """trade 的順序寫死：對帳 → 決策送單（含等成交）→ 當日結算；任一前段失敗即停"""
    D = "2026-09-22"
    DECISION = {"type": "decision", "date": "2026-09-22"}

    def setUp(self):
        for name in ("notify", "log"):
            p = mock.patch.object(daily, name, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(daily, "in_trade_window", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def run_trade(self, run_rc, journals, decide_rc=0):
        calls = []

        def fake_run(mode, *args):
            calls.append(args)
            return run_rc

        with mock.patch.object(daily, "run", fake_run), \
             mock.patch.object(daily, "load_journal", side_effect=journals), \
             mock.patch.object(daily, "_decide_steps", return_value=decide_rc) as ds:
            rc = daily.trade(self.D, "live")
        return rc, calls, ds

    def test_對帳失敗就不進決策(self):
        rc, calls, ds = self.run_trade(run_rc=1, journals=[[]])
        self.assertEqual(rc, 1)
        self.assertEqual([c[0] for c in calls], [daily.SYNC])
        ds.assert_not_called()

    def test_一場順序_對帳_決策_當日結算(self):
        rc, calls, ds = self.run_trade(run_rc=0, journals=[[], [self.DECISION]])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], daily.SYNC)
        ds.assert_called_once_with("trade", self.D, "live", True, 30)
        settle = next(c for c in calls if str(c[0]).endswith("journal.py"))
        self.assertEqual(settle[1:], ("settle", "--same-day"))
        self.assertTrue(str(calls[-1][0]).endswith("record_return.py"))

    def test_當日已有決策不再交易(self):
        rc, calls, ds = self.run_trade(run_rc=0, journals=[[self.DECISION]])
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [])
        ds.assert_not_called()

    def test_邊界全面阻擋時沒有決策也不結算(self):
        rc, calls, _ = self.run_trade(run_rc=0, journals=[[], []])
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], [daily.SYNC])


class TestSettleStepsSameDay(unittest.TestCase):
    def test_當日結算把same_day旗標傳給journal(self):
        calls = []
        with mock.patch.object(daily, "run", lambda mode, *a: calls.append(a) or 0), \
             mock.patch.object(daily, "log", lambda *a, **k: None):
            daily._settle_steps("trade", same_day=True)
        self.assertEqual(calls[0][1:], ("settle", "--same-day"))

    def test_排程結算不帶same_day(self):
        calls = []
        with mock.patch.object(daily, "run", lambda mode, *a: calls.append(a) or 0), \
             mock.patch.object(daily, "log", lambda *a, **k: None):
            daily._settle_steps("settle")
        self.assertEqual(calls[0][1:], ("settle",))


class TestHolidaySkip(unittest.TestCase):
    """packet 回報休市（離開碼 3）＝今天不交易，不是故障；不可往下用舊行情做決策。"""

    def steps(self, packet_rc):
        calls = []

        def fake_run(mode, *args):
            calls.append(os.path.basename(args[0]))
            return packet_rc if args[0].endswith("packet.py") else 0
        with mock.patch.object(daily, "run", fake_run), \
             mock.patch.object(daily, "notify", lambda *a, **k: None), \
             mock.patch.object(daily, "ask_model") as model:
            rc = daily._decide_steps("decide", "2026-10-09", "simulation", True, None)
        return rc, calls, model

    def test_休市_跳過且不跑風控與決策層(self):
        rc, calls, model = self.steps(daily.PACKET_MARKET_CLOSED)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["packet.py"])
        model.assert_not_called()

    def test_決策包其他失敗仍是故障(self):
        rc, calls, model = self.steps(1)
        self.assertEqual(rc, 1)
        model.assert_not_called()


class TestDecidePromptFieldSemantics(unittest.TestCase):
    """suggest_qty 是建議調節（賣出）股數，不是持有目標；燈號必賣由 lamp_exits 給出。"""

    def text(self, name):
        with open(os.path.join(os.path.dirname(daily.__file__), "prompts", name),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_決策提示寫明欄位語意與燈號規則(self):
        text = daily.render_prompt(self.text("decide.md"), "P", "E", "D", "2026-10-12")
        for needle in ("建議調節（賣出）股數", "不是持有目標", "持有 − suggest_qty",
                       "lamp_exits", "確認無燈號", "unknown", "未經複盤", "只適用減碼"):
            self.assertIn(needle, text)

    def test_複盤提示要求處理待複盤事項(self):
        text = daily.render_review_prompt(self.text("review.md"), "R", "RULES", "DOC", "2026-10-09")
        self.assertIn("待複盤事項", text)


class TestReviewSince(unittest.TestCase):
    """複盤區間從上次**成功**複盤的隔天起算——某次複盤失敗時，那幾天的案例
    不會永遠掉出 7 天視窗（2026-10-02 實錄）。輸入是已完成的複盤迄日。"""

    def test_從上次成功複盤隔天起算(self):
        self.assertEqual(daily.review_since(["2026-09-04", "2026-09-27"], "2026-10-09"),
                         "2026-09-28")

    def test_沒有前次複盤_用預設視窗(self):
        self.assertIsNone(daily.review_since([], "2026-10-09"))

    def test_上次複盤太久以前_最多回看28天(self):
        self.assertEqual(daily.review_since(["2026-06-01"], "2026-10-09"), "2026-09-12")

    def test_上次複盤就在近期_不比預設視窗短(self):
        self.assertIsNone(daily.review_since(["2026-10-08"], "2026-10-09"))


class TestCompletedReviews(unittest.TestCase):
    """哪些複盤算完成：成功紀錄（completed.jsonl）為準；成功紀錄機制上線前的
    檢討檔（9/04、9/11、9/27）保守兼容。之後只有檢討檔、沒有成功紀錄的＝失敗嘗試。"""

    DOCS = ["2026-09-04.md", "2026-09-27.md", "2026-10-08-learning.md", "README.md"]

    def test_上線前的檢討檔算完成(self):
        self.assertEqual(daily.completed_reviews([], self.DOCS), ["2026-09-04", "2026-09-27"])

    def test_上線後只有檢討檔沒有成功紀錄_不算完成(self):
        done = daily.completed_reviews([], self.DOCS + ["2026-10-09.md"])
        self.assertNotIn("2026-10-09", done)
        self.assertEqual(daily.review_since(done, "2026-10-16"), "2026-09-28")

    def test_成功紀錄推進起日(self):
        done = daily.completed_reviews([{"until": "2026-10-09", "ok": True}],
                                       self.DOCS + ["2026-10-09.md"])
        self.assertEqual(done[-1], "2026-10-09")
        self.assertEqual(daily.review_since(done, "2026-10-23"), "2026-10-10")

    def test_壞紀錄不算(self):
        done = daily.completed_reviews([{"until": "2026-10-09", "ok": False}, {"ts": "x"},
                                        {"until": "not-a-date", "ok": True}], [])
        self.assertEqual(done, [])


class TestReviewRecordsOnlyOnSuccess(unittest.TestCase):
    """失敗的複盤嘗試即使已寫出檢討檔，也不能留下成功紀錄、不能推進下次起日。"""

    DATE = "2026-10-09"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        self.docs = os.path.join(self.tmp, "docs")
        os.makedirs(os.path.join(self.docs, "decision-reviews"))
        self.rules = os.path.join(self.docs, "decision-rules.md")
        with open(self.rules, "w", encoding="utf-8") as fh:
            fh.write("# 決策準則\n\n### R-001 甲\n內容\n")
        with open(os.path.join(self.docs, "decision-reviews", "2026-09-27.md"), "w") as fh:
            fh.write("舊的已完成複盤")

    def run_review(self, rc, break_rules=False):
        import packet
        import gates
        doc = daily.review_doc_path(self.rules, self.DATE)

        def fake_model(mode, prompt, model, tools=None, cwd=None):
            with open(doc, "w", encoding="utf-8") as fh:
                fh.write("模型先寫出的檢討記錄")
            if break_rules:
                with open(self.rules, "w", encoding="utf-8") as fh:
                    fh.write("# 決策準則\n（編號被刪光）\n")
            return rc, "結論"
        noop = lambda *a, **k: None
        with mock.patch.object(daily, "STATE", self.state), \
             mock.patch.object(daily, "in_window", return_value=True), \
             mock.patch.object(daily, "run", lambda mode, *a: 0), \
             mock.patch.object(daily, "ask_model", fake_model), \
             mock.patch.object(daily, "notify", noop), \
             mock.patch.object(daily, "log", noop), \
             mock.patch.object(daily, "commit_files", noop), \
             mock.patch.object(packet, "RULES_PATH", self.rules), \
             mock.patch.object(gates, "default_path",
                               return_value=os.path.join(self.tmp, "gates.json")):
            result = daily.review(self.DATE)
            records = daily.load_review_records()
        done = daily.completed_reviews(records, os.listdir(os.path.dirname(doc)))
        return result, records, done, os.path.exists(doc)

    def test_模型非零離開_檔案留著但不算完成(self):
        result, records, done, doc_left = self.run_review(rc=1)
        self.assertEqual(result, 1)
        self.assertTrue(doc_left)                      # 不刪失敗檔
        self.assertEqual(records, [])
        self.assertEqual(daily.review_since(done, "2026-10-16"), "2026-09-28")

    def test_準則被改壞_還原且不算完成(self):
        result, records, done, doc_left = self.run_review(rc=0, break_rules=True)
        self.assertEqual(result, 1)
        self.assertTrue(doc_left)
        self.assertEqual(records, [])
        self.assertIn("R-001", open(self.rules, encoding="utf-8").read())
        self.assertEqual(daily.review_since(done, "2026-10-16"), "2026-09-28")

    def test_成功才寫紀錄_記下涵蓋區間(self):
        result, records, done, _ = self.run_review(rc=0)
        self.assertEqual(result, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["until"], self.DATE)
        self.assertEqual(records[0]["since"], "2026-09-28")
        self.assertTrue(records[0]["ok"])
        self.assertEqual(done[-1], self.DATE)


class TestDecisionCli(unittest.TestCase):
    """ARK_DECISION_CLI：claude（預設）或 codex。不論哪支，決策層都只產出決策 JSON，
    送單永遠是後面 execute.py 的事——codex 那支一定是唯讀沙箱。"""

    PROMPT = "你是 ark-agent 的決策層"

    def test_claude指令與原本相同(self):
        cmd = daily.model_command("claude", self.PROMPT, "fable", daily.DECIDE_TOOLS)
        self.assertEqual(cmd, ["claude", "-p", self.PROMPT, "--model", "fable",
                               "--allowedTools", "Read", "Write", "WebSearch", "WebFetch",
                               "--permission-mode", "acceptEdits", "--output-format", "text"])
        self.assertNotIn("Bash", cmd)

    def test_codex用exec_唯讀沙箱_不讀個人設定(self):
        cmd = daily.model_command("codex", self.PROMPT, "gpt-5", daily.DECIDE_TOOLS)
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5")

    def test_codex推理強度寫死xhigh(self):
        """實測：--ignore-user-config 下推理強度預設是 none"""
        for model in (None, "gpt-5"):
            cmd = daily.model_command("codex", self.PROMPT, model, daily.DECIDE_TOOLS)
            overrides = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "-c"]
            self.assertIn('model_reasoning_effort="xhigh"', overrides)
            self.assertIn('web_search="live"', overrides)
        self.assertEqual(cmd[-1], self.PROMPT)

    def test_codex未設模型就不帶m(self):
        cmd = daily.model_command("codex", self.PROMPT, None, daily.DECIDE_TOOLS)
        self.assertNotIn("-m", cmd)
        self.assertNotIn("--model", cmd)

    def test_codex決策層不得有寫入或免沙箱旗標(self):
        """不變量：決策層只輸出 JSON。寫入沙箱或略過沙箱，模型就有機會自己送單"""
        for model in (None, "gpt-5"):
            cmd = daily.model_command("codex", self.PROMPT, model, daily.DECIDE_TOOLS)
            joined = " ".join(cmd[:-1])                  # 最後一個是提示本文
            for banned in ("workspace-write", "danger-full-access",
                           "--dangerously-bypass-approvals-and-sandbox", "--yolo",
                           "--full-auto", "--approve-for-me", "--not-so-yolo"):
                self.assertNotIn(banned, joined)
            self.assertEqual(cmd.count("--sandbox"), 1)
            self.assertNotIn("-s", cmd)

    def test_codex不接複盤的寫入工具(self):
        with self.assertRaises(ValueError):
            daily.model_command("codex", self.PROMPT, None, daily.REVIEW_TOOLS)

    def test_未知CLI報錯(self):
        with self.assertRaises(ValueError):
            daily.model_command("gemini", self.PROMPT, None, daily.DECIDE_TOOLS)

    def test_預設值_未設時走claude與fable(self):
        self.assertEqual(daily.model_settings({}), ("claude", "fable", "fable"))
        self.assertEqual(daily.model_settings({"ARK_DECISION_CLI": " "}),
                         ("claude", "fable", "fable"))

    def test_claude模式沿用ARK_DECISION_MODEL(self):
        self.assertEqual(daily.model_settings({"ARK_DECISION_MODEL": "opus"}),
                         ("claude", "opus", "opus"))

    def test_codex未設模型就用Codex預設(self):
        self.assertEqual(daily.model_settings({"ARK_DECISION_CLI": "codex"}),
                         ("codex", None, "fable"))

    def test_codex模式下複盤仍用claude的預設模型(self):
        self.assertEqual(daily.model_settings({"ARK_DECISION_CLI": "codex",
                                               "ARK_DECISION_MODEL": "gpt-5"}),
                         ("codex", "gpt-5", "fable"))

    def test_不認得的CLI在任何一步之前擋下(self):
        """trade 第一步就會寫 ARK App，設定錯不該走到那裡"""
        for mode in ("decide", "trade", "check"):
            with mock.patch.object(daily, "DECISION_CLI", "gemini"), \
                 mock.patch.object(daily.sys, "argv", ["daily.py", mode]), \
                 mock.patch.object(daily, "run") as run, \
                 mock.patch.object(daily, "notify", lambda *a, **k: None):
                self.assertEqual(daily.main(), 1, mode)
            run.assert_not_called()

    def test_preflight分流(self):
        self.assertEqual(daily.required_clis("claude"), (["claude"], []))
        self.assertEqual(daily.required_clis("codex"), (["codex"], ["claude"]))


class TestParseDecisionOutput(unittest.TestCase):
    def test_純JSON(self):
        self.assertEqual(daily.parse_decision_output('\n{"no_trade": true, "orders": []}\n'),
                         {"no_trade": True, "orders": []})

    def test_整段包在fence裡也認(self):
        self.assertEqual(daily.parse_decision_output('```json\n{"a": 1}\n```'), {"a": 1})

    def test_夾雜說明文字不認(self):
        with self.assertRaises(ValueError):
            daily.parse_decision_output('決策如下：\n{"a": 1}')

    def test_非物件不認(self):
        with self.assertRaises(ValueError):
            daily.parse_decision_output("[1, 2]")
        with self.assertRaises(ValueError):
            daily.parse_decision_output("")


class TestCheckFollowsDecisionCli(unittest.TestCase):
    """前置檢查找的是決策層用的那支 CLI；codex 模式缺 claude 只提醒（複盤要用），不擋"""

    def run_check(self, cli, present):
        notes, logs = [], []
        with mock.patch.object(daily, "DECISION_CLI", cli), \
             mock.patch.object(daily, "has_cli", side_effect=lambda n: n in present), \
             mock.patch.object(daily, "run", return_value=0), \
             mock.patch.object(daily, "notify", lambda mode, msg: notes.append(msg)), \
             mock.patch.object(daily, "log", lambda mode, msg: logs.append(msg)), \
             mock.patch.object(daily.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            rc = daily.check("simulation")
        return rc, notes, logs

    def test_claude模式找claude(self):
        rc, notes, _ = self.run_check("claude", {"codex"})
        self.assertEqual(rc, 1)
        self.assertIn("找不到 claude", notes[-1])

    def test_codex模式找codex(self):
        rc, notes, _ = self.run_check("codex", {"claude"})
        self.assertEqual(rc, 1)
        self.assertIn("找不到 codex", notes[-1])

    def test_codex模式缺claude只提醒(self):
        rc, notes, logs = self.run_check("codex", {"codex"})
        self.assertEqual(rc, 0)
        self.assertTrue(any("claude" in m and "review" in m for m in logs))


class TestCodexDecideStep(unittest.TestCase):
    """codex 的決策 JSON 從 stdout 來，由 daily.py 代寫決策檔，再交給 journal 驗"""

    DATE = "2026-10-16"

    def setUp(self):
        self.state = tempfile.mkdtemp()
        for sub in ("packets", "envelopes"):
            os.makedirs(os.path.join(self.state, sub))
            with open(os.path.join(self.state, sub, f"{self.DATE}.json"), "w") as fh:
                fh.write("{}")

    def decide(self, output, rc=0):
        calls, prompts = [], []

        def fake_model(mode, prompt, model, tools=None, cwd=None, cli=None):
            prompts.append((prompt, model, cli))
            return rc, output
        with mock.patch.object(daily, "STATE", self.state), \
             mock.patch.object(daily, "DECISION_CLI", "codex"), \
             mock.patch.object(daily, "DECISION_MODEL", None), \
             mock.patch.object(daily, "run", lambda mode, *a: calls.append(a) or 0), \
             mock.patch.object(daily, "ask_model", fake_model), \
             mock.patch.object(daily, "notify", lambda *a, **k: None):
            result = daily._decide_steps("decide", self.DATE, "simulation", False, None)
        return result, calls, prompts

    def decision_path(self):
        return os.path.join(self.state, "decisions", f"{self.DATE}.json")

    def test_stdout的JSON寫成決策檔再交journal驗(self):
        result, calls, prompts = self.decide('{"date": "2026-10-16", "no_trade": true, "orders": []}')
        self.assertEqual(result, 0)
        with open(self.decision_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["no_trade"], True)
        self.assertEqual(calls[-1][1:], ("record", self.decision_path()))
        prompt, model, cli = prompts[0]
        self.assertEqual(cli, "codex")
        self.assertIsNone(model)
        self.assertIn("唯讀沙箱", prompt)
        self.assertIn(self.decision_path(), prompt)

    def test_輸出不是JSON就停_不寫檔不送單(self):
        result, calls, _ = self.decide("我決定今天不動。")
        self.assertEqual(result, 1)
        self.assertFalse(os.path.exists(self.decision_path()))
        self.assertFalse(any(str(c[0]).endswith(("journal.py", "execute.py")) for c in calls))

    def test_codex非零離開不降級(self):
        """接手模型是 claude 的模型名，codex 撞牆時不能拿它重試"""
        result, _, prompts = self.decide("/usage-credits", rc=1)
        self.assertEqual(result, 1)
        self.assertEqual(len(prompts), 1)


class TestAskModelProcess(unittest.TestCase):
    """實測：codex exec 見到非 TTY 的 stdin 會讀到 EOF 才開始，繼承到不會關的 pipe 就永遠卡住"""

    def call(self, cli):
        proc = mock.Mock(returncode=0, stdout='{"a": 1}', stderr="進度")
        with mock.patch.object(daily.subprocess, "run", return_value=proc) as run, \
             mock.patch.object(daily, "log", lambda *a, **k: None):
            rc, out = daily.ask_model("decide", "提示", None if cli == "codex" else "fable",
                                      cli=cli)
        return rc, out, run.call_args.kwargs

    def test_stdin一律接devnull(self):
        for cli in ("claude", "codex"):
            _, _, kw = self.call(cli)
            self.assertIs(kw["stdin"], daily.subprocess.DEVNULL, cli)

    def test_codex的stderr不混進回傳的決策輸出(self):
        rc, out, kw = self.call("codex")
        self.assertIs(kw["stderr"], daily.subprocess.PIPE)
        self.assertEqual(out, '{"a": 1}')

    def test_找不到CLI回127(self):
        with mock.patch.object(daily.subprocess, "run", side_effect=FileNotFoundError), \
             mock.patch.object(daily, "log", lambda *a, **k: None):
            self.assertEqual(daily.ask_model("decide", "提示", None, cli="codex"), (127, ""))
