"""風控層純邏輯測試（不需要 ARK 或 Shioaji，任何平台可跑）"""
import json
import os
import tempfile
import unittest

import equity
import gates
import risk
import tracks

# 2026-08 的交易日曆（跳過 8/15、8/16 週末）
CALENDAR = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
            "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
            "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]


def P(qty, avg_price, last_price, pnl):
    return {"qty": qty, "avg_price": avg_price, "last_price": last_price, "pnl": pnl}


POSITIONS = {
    "2330": P(20, 2062.38, 2380.0, 6352.4),     # 市值 47,600　成本 41,247.6　roi +15.4%
    "2308": P(30, 1822.57, 1815.0, -227.1),     # 市值 54,450　虧損
    "0050": P(900, 96.76, 104.25, 6741.0),      # 市值 93,825
    "0053": P(2, 245.0, 237.0, -16.0),          # 市值 474　低於門檻
}
ASSIGNED = {"2330": tracks.SATELLITE, "2308": tracks.INHERITED}


def make_packet(suggested_ratio=66.5, stock_value=196349.0, cash=14673.0,
                adjust_amount=45882.0, sync_ok=True, positions=None, industry=None):
    return {
        "schema": 1, "date": "2026-08-11", "hash": "sha256:x",
        "ark": {
            "holdings": {},
            "posture": {"suggested_ratio": suggested_ratio, "actual_ratio": 93.05,
                        "gap": 26.55, "stock_value": stock_value, "cash": cash,
                        "suggested_value": 0.0, "suggested_cash": 0.0,
                        "adjust_amount": adjust_amount},
            "layout": None,
        },
        "account": {"positions": POSITIONS if positions is None else positions,
                    "balance": cash, "settlements": {}, "sync_ok": sync_ok, "diff": []},
        "market": {"quotes": {c: {"close": p["last_price"]}
                              for c, p in (POSITIONS if positions is None
                                           else positions).items()}},
        "discipline": {"max_names": 2, "adjust_required_before_buy": True,
                       "adjust_amount": adjust_amount, "sellable": [], "sell_priority": [],
                       "buy_candidates": []},
        "news_scope": {"codes": []},
        "industry": industry or {"source": None, "candidates": {}, "sectors": {}, "error": None},
    }


class TestMaxNamesHysteresis(unittest.TestCase):
    def test_首次無既有值直接採用當日(self):
        self.assertEqual(risk.max_names_with_hysteresis(None, [2], days=5), 2)

    def test_連續天數不足維持原值(self):
        self.assertEqual(risk.max_names_with_hysteresis(1, [1, 2, 2, 2], days=5), 1)

    def test_歷史長度不足時即使全部一致也不變更(self):
        """剛開始記錄就讓檔數跳動等於沒有遲滯——必須先湊滿觀察天數"""
        self.assertEqual(risk.max_names_with_hysteresis(1, [2, 2, 2], days=5), 1)

    def test_連續達標才變更(self):
        self.assertEqual(risk.max_names_with_hysteresis(1, [1, 2, 2, 2, 2, 2], days=5), 2)

    def test_期間跳動一次就重新計數(self):
        """資產在 10 萬門檻附近震盪時，跳動的那天必須讓計數歸零"""
        self.assertEqual(risk.max_names_with_hysteresis(1, [2, 2, 1, 2, 2, 2], days=5), 1)

    def test_與既有值相同不變更(self):
        self.assertEqual(risk.max_names_with_hysteresis(2, [2, 2, 2, 2, 2], days=5), 2)

    def test_空歷史維持原值(self):
        self.assertEqual(risk.max_names_with_hysteresis(3, [], days=5), 3)


class TestRatioBandAction(unittest.TestCase):
    def test_實際高於建議超過帶寬要降(self):
        self.assertEqual(risk.ratio_band_action(66.5, 93.0, band_pp=5.0), risk.REDUCE)

    def test_實際低於建議超過帶寬可買(self):
        self.assertEqual(risk.ratio_band_action(66.5, 55.0, band_pp=5.0), risk.BUY)

    def test_帶寬內不動作(self):
        self.assertEqual(risk.ratio_band_action(66.5, 68.0, band_pp=5.0), risk.HOLD)

    def test_恰好等於帶寬仍不動作(self):
        """無動作帶含端點——邊界上為了 0.0 個百分點付一次來回成本不划算"""
        self.assertEqual(risk.ratio_band_action(66.5, 71.5, band_pp=5.0), risk.HOLD)
        self.assertEqual(risk.ratio_band_action(66.5, 61.5, band_pp=5.0), risk.HOLD)


class TestConcentrationBreaches(unittest.TestCase):
    def breaches(self, positions=None, cap=0.35):
        resolved = tracks.resolve(ASSIGNED, positions or POSITIONS, 3000.0)
        return risk.concentration_breaches(resolved, total=211022.0, cap=cap)

    def test_超過上限列出並附超額金額(self):
        b = {x["code"]: x for x in self.breaches(cap=0.35)}
        self.assertIn("0050", b)                                  # 93,825 / 211,022 = 44.5%
        self.assertAlmostEqual(b["0050"]["ratio"], 0.44462, places=4)
        self.assertAlmostEqual(b["0050"]["excess_value"], 19967.3, places=1)

    def test_未超過不列(self):
        self.assertEqual(self.breaches(cap=0.60), [])

    def test_繼承與凍結軌不列(self):
        """2308 佔 25.8% 但虧損中動不了，列出來只會製造做不到的待辦"""
        codes = [x["code"] for x in self.breaches(cap=0.20)]
        self.assertNotIn("2308", codes)
        self.assertNotIn("0053", codes)
        self.assertIn("2330", codes)

    def test_獲利者標為可處置(self):
        b = {x["code"]: x for x in self.breaches(cap=0.20)}
        self.assertTrue(b["2330"]["actionable"])

    def test_虧損者標為不可處置(self):
        pos = {**POSITIONS, "2330": P(20, 2062.38, 2380.0, -500.0)}
        b = {x["code"]: x for x in self.breaches(positions=pos, cap=0.20)}
        self.assertFalse(b["2330"]["actionable"])

    def test_依超額由大到小排序(self):
        b = self.breaches(cap=0.10)
        self.assertEqual([x["code"] for x in b], ["0050", "2330"])


class TestStopLossCandidates(unittest.TestCase):
    def candidates(self, positions, stop=-0.12):
        resolved = tracks.resolve(ASSIGNED, positions, 3000.0)
        return risk.stop_loss_candidates(resolved, positions, stop_pct=stop)

    def test_衛星軌跌破停損列出(self):
        pos = {**POSITIONS, "2330": P(20, 2062.38, 1800.0, -5247.6)}
        c = self.candidates(pos)
        self.assertEqual([x["code"] for x in c], ["2330"])
        self.assertAlmostEqual(c[0]["roi"], -0.12722, places=4)

    def test_恰好等於門檻即觸發(self):
        pos = {**POSITIONS, "2330": P(20, 2000.0, 1760.0, -4800.0)}   # roi = -12.0%
        self.assertEqual([x["code"] for x in self.candidates(pos)], ["2330"])

    def test_未跌破不列(self):
        self.assertEqual(self.candidates(POSITIONS), [])

    def test_主軌虧損不列(self):
        """主軌守 ARK『虧損不賣』，停損只存在於衛星軌"""
        pos = {**POSITIONS, "0050": P(900, 96.76, 60.0, -33084.0)}
        self.assertEqual(self.candidates(pos), [])

    def test_繼承軌虧損不列(self):
        pos = {**POSITIONS, "2308": P(30, 1822.57, 1000.0, -24677.1)}
        self.assertEqual(self.candidates(pos), [])

    def test_成本為零不除以零(self):
        """現價拉高到凍結門檻之上，才真的走到除法那一行"""
        pos = {"2330": P(20, 0.0, 200.0, 2000.0)}
        self.assertEqual(self.candidates(pos), [])


class TestSatelliteQuota(unittest.TestCase):
    def test_配額為投入金額的一半(self):
        # 總資源 200,000 × 建議 70% × 0.5 = 70,000
        self.assertAlmostEqual(risk.satellite_quota(200000.0, 70.0, 0.5, 0.25), 70000.0)

    def test_讀不到建議比例時退用固定比例(self):
        self.assertAlmostEqual(risk.satellite_quota(200000.0, None, 0.5, 0.25), 50000.0)


class TestTrimCandidates(unittest.TestCase):
    def test_超過上限加緩衝才列(self):
        resolved = {"2308": {"track": tracks.SATELLITE, "value": 55000.0, "pnl": 400.0},
                    "2330": {"track": tracks.SATELLITE, "value": 28000.0, "pnl": 3000.0},
                    "0050": {"track": tracks.CORE, "value": 90000.0, "pnl": 0.0}}
        out = risk.trim_candidates(resolved, total=200000.0, name_cap=0.12, buffer=0.03)
        self.assertEqual([c["code"] for c in out], ["2308"])          # 27.5% > 15%；2330 14% 不列
        self.assertAlmostEqual(out[0]["target_value"], 24000.0)
        self.assertAlmostEqual(out[0]["excess_value"], 31000.0)

    def test_主軌不列(self):
        resolved = {"0050": {"track": tracks.CORE, "value": 90000.0, "pnl": 0.0}}
        self.assertEqual(risk.trim_candidates(resolved, 100000.0, 0.12, 0.03), [])


class TestSatelliteAllowlist(unittest.TestCase):
    def test_候選聯集持有與指定(self):
        resolved = {"2330": {"track": tracks.SATELLITE}, "0050": {"track": tracks.CORE}}
        out = risk.satellite_allowlist({"2308": tracks.SATELLITE}, resolved,
                                       {"6274": {}, "2330": {}})
        self.assertEqual(out, ["2308", "2330", "6274"])


class TestSectorExposure(unittest.TestCase):
    def test_依產業加總衛星軌市值_缺產業別各自成一類(self):
        resolved = {"2330": {"track": tracks.SATELLITE, "value": 44000.0},
                    "2308": {"track": tracks.SATELLITE, "value": 55000.0},
                    "6274": {"track": tracks.SATELLITE, "value": 10000.0},
                    "0050": {"track": tracks.CORE, "value": 9000.0}}
        out = risk.sector_exposure(resolved, {"2330": "半導體", "6274": "PCB-製造"})
        self.assertEqual(out, {"半導體": 44000.0, "PCB-製造": 10000.0, "2308": 55000.0})


class TestConsecutiveStops(unittest.TestCase):
    def test_由尾端計算連續次數(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True}]
        self.assertEqual(risk.consecutive_stops(exits), 2)

    def test_中間有非停損出場則歸零重算(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": False},
                 {"date": "2026-08-12", "stopped": True}]
        self.assertEqual(risk.consecutive_stops(exits), 1)

    def test_空清單為零(self):
        self.assertEqual(risk.consecutive_stops([]), 0)

    def test_最後一筆非停損為零(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": False}]
        self.assertEqual(risk.consecutive_stops(exits), 0)


class TestTradingDayOffset(unittest.TestCase):
    def test_偏移以交易日計不含週末(self):
        self.assertEqual(risk.trading_day_offset(CALENDAR, "2026-08-13", 3),
                         "2026-08-18")

    def test_日期不在日曆上取其後第一個交易日(self):
        self.assertEqual(risk.trading_day_offset(CALENDAR, "2026-08-15", 1),
                         "2026-08-18")

    def test_超出日曆回None(self):
        self.assertIsNone(risk.trading_day_offset(CALENDAR, "2026-08-27", 10))


class TestSatelliteCooldown(unittest.TestCase):
    def cooldown(self, exits, today, streak=3, days=10):
        return risk.satellite_cooldown(exits, streak_cap=streak, cooldown_days=days,
                                       calendar=CALENDAR, today=today)

    def test_未達連續次數不停(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True}]
        halted, _reason, until = self.cooldown(exits, "2026-08-12")
        self.assertFalse(halted)
        self.assertIsNone(until)

    def test_達連續次數後冷卻期內停止新倉(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True},
                 {"date": "2026-08-12", "stopped": True}]
        halted, reason, until = self.cooldown(exits, "2026-08-13", days=5)
        self.assertTrue(halted)
        self.assertEqual(until, "2026-08-19")
        self.assertIn("連續", reason)

    def test_冷卻期滿解除(self):
        exits = [{"date": "2026-08-10", "stopped": True},
                 {"date": "2026-08-11", "stopped": True},
                 {"date": "2026-08-12", "stopped": True}]
        halted, _reason, _until = self.cooldown(exits, "2026-08-19", days=5)
        self.assertFalse(halted)

    def test_空紀錄不停(self):
        self.assertEqual(self.cooldown([], "2026-08-12")[0], False)


class TestOrderCaps(unittest.TestCase):
    LIMITS = {"per_order_cap": 20000.0, "daily_buy_cap": 40000.0,
              "daily_turnover_cap": 60000.0, "min_trade_value": 3000.0}
    QUOTES = {"0050": {"close": 100.0}, "2330": {"close": 2380.0}}

    def caps(self, orders):
        return risk.order_caps_violations(orders, self.QUOTES, self.LIMITS)

    def test_全部合規回空(self):
        self.assertEqual(self.caps([{"action": "buy", "code": "0050", "qty": 100}]), [])

    def test_單筆超限(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 250}])   # 25,000
        self.assertEqual(len(v), 1)
        self.assertIn("單筆", v[0])

    def test_賣單不受單筆上限(self):
        """金額上限只管買進側：賣出是調節既有部位，上界天然受持倉量與
        「獲利才調節」紀律約束，設上限會把 ARK 要求的調節卡成好幾天。"""
        v = self.caps([{"action": "sell", "code": "2330", "qty": 24}])    # 57,120
        self.assertEqual(v, [])

    def test_賣單不計入單日成交(self):
        v = self.caps([{"action": "sell", "code": "2330", "qty": 42}])    # 99,960
        self.assertEqual(v, [])

    def test_單日買進總額超限(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 190},
                       {"action": "buy", "code": "2330", "qty": 10}])     # 19,000 + 23,800
        self.assertTrue(any("單日買進" in x for x in v))

    def test_單日成交總額超限(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 190},     # 19,000 ×3
                       {"action": "buy", "code": "0050", "qty": 190},
                       {"action": "buy", "code": "0050", "qty": 190},
                       {"action": "buy", "code": "2330", "qty": 8}])      # 19,040
        self.assertTrue(any("單日成交" in x for x in v))

    def test_低於最小可交易金額(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 10}])     # 1,000
        self.assertTrue(any("最小可交易" in x for x in v))

    def test_賣單低於最小可交易金額仍報(self):
        """最小可交易金額是成本佔比問題，與方向無關，賣單照查。"""
        v = self.caps([{"action": "sell", "code": "0050", "qty": 10}])    # 1,000
        self.assertTrue(any("最小可交易" in x for x in v))

    def test_凍結軌清倉不受最小可交易金額限制(self):
        """凍結軌就是低於門檻的部位，清倉必然低於門檻；journal 已驗過是整筆清掉。"""
        v = self.caps([{"action": "sell", "code": "0050", "qty": 10, "track": "frozen"}])
        self.assertEqual(v, [])

    def test_凍結軌標記不放寬買單(self):
        v = self.caps([{"action": "buy", "code": "0050", "qty": 10, "track": "frozen"}])
        self.assertTrue(any("最小可交易" in x for x in v))

    def test_無報價時以限價中點估值(self):
        v = self.caps([{"action": "buy", "code": "9999", "qty": 10,
                        "limit_low": 2400.0, "limit_high": 2600.0}])      # 25,000
        self.assertTrue(any("單筆" in x for x in v))


class TestTotalResource(unittest.TestCase):
    def test_有運算頁讀值時以其為準(self):
        self.assertEqual(risk.total_resource(make_packet()), 211022.0)

    def test_無運算頁讀值時以持倉加餘額推算(self):
        pk = make_packet()
        pk["ark"]["posture"] = None
        self.assertAlmostEqual(risk.total_resource(pk), 211022.0, places=0)


class TestBuildEnvelope(unittest.TestCase):
    def env(self, packet=None, points=None, exits=(), config=None, effective_names=2):
        return risk.build_envelope(
            packet=packet or make_packet(),
            assigned=ASSIGNED,
            equity_points=list(points if points is not None
                               else [equity.make_point("2026-08-10", 196349.0,
                                                       14673.0, 47600.0)]),
            satellite_exits=list(exits),
            calendar=CALENDAR,
            today="2026-08-11",
            effective_max_names=effective_names,
            max_names_history=[2],
            config={**risk.DEFAULTS, **(config or {})})

    def test_正常狀態可買可賣(self):
        e = self.env()
        self.assertTrue(e["can_buy"])
        self.assertTrue(e["can_sell"])
        self.assertEqual(e["blocks"], [])
        self.assertEqual(e["breaker"]["level"], equity.NONE)

    def test_L1熔斷停買仍可賣(self):
        points = [equity.make_point("2026-08-10", 200000.0, 11022.0, 47600.0),
                  equity.make_point("2026-08-11", 180000.0, 11022.0, 47600.0)]
        e = self.env(points=points)
        self.assertEqual(e["breaker"]["level"], equity.L1)
        self.assertFalse(e["can_buy"])
        self.assertTrue(e["can_sell"])

    def test_L2熔斷全停(self):
        points = [equity.make_point("2026-08-10", 200000.0, 11022.0, 47600.0),
                  equity.make_point("2026-08-11", 160000.0, 11022.0, 47600.0)]
        e = self.env(points=points)
        self.assertEqual(e["breaker"]["level"], equity.L2)
        self.assertFalse(e["can_buy"])
        self.assertFalse(e["can_sell"])
        self.assertTrue(any("L2" in b for b in e["blocks"]))

    def test_運算頁讀不到時全停(self):
        """隱私眼睛開啟會遮蔽金額，read_posture 靜默回 None，discipline 隨即
        退化成 cash=0、不需調節——建立在錯的事實上遠比不交易危險"""
        pk = make_packet()
        pk["ark"]["posture"] = None
        e = self.env(packet=pk)
        self.assertFalse(e["can_buy"])
        self.assertFalse(e["can_sell"])
        self.assertTrue(any("運算頁" in b for b in e["blocks"]))

    def test_對帳不一致全停(self):
        """紀律邊界建立在錯的持倉上，比不交易還危險"""
        e = self.env(packet=make_packet(sync_ok=False))
        self.assertFalse(e["can_buy"])
        self.assertFalse(e["can_sell"])
        self.assertTrue(any("對帳" in b for b in e["blocks"]))

    def test_衛星軌白名單來自指定而非持倉(self):
        """白名單＝ARK 候選 ∪ 目前持有的衛星軌 ∪ tracks.json 指定：持有與指定一定要在，
        減碼與停損不能被白名單擋；這裡沒有候選池，00631L 純靠手動指定才出現在白名單。"""
        e = risk.build_envelope(
            packet=make_packet(), assigned={**ASSIGNED, "00631L": tracks.SATELLITE},
            equity_points=[equity.make_point("2026-08-10", 196349.0, 14673.0, 47600.0)],
            satellite_exits=[], calendar=CALENDAR, today="2026-08-11",
            effective_max_names=2, max_names_history=[2], config=risk.DEFAULTS)
        s = e["tracks"]["satellite"]
        self.assertEqual(s["allowlist"], ["00631L", "2330"])
        self.assertEqual(s["codes"], ["2330"])          # 白名單 ≠ 已持有

    def test_衛星軌配額不含繼承部位(self):
        """2308 的 54,450 若計入配額，衛星軌開場就爆表且永遠買不了"""
        s = self.env()["tracks"]["satellite"]
        self.assertEqual(s["value"], 47600.0)
        self.assertAlmostEqual(s["quota"], 52755.5, places=1)     # 211,022 × 25%
        self.assertAlmostEqual(s["remaining"], 5155.5, places=1)

    def test_四軌市值加總等於持倉總市值(self):
        t = self.env()["tracks"]
        total = sum(t[k]["value"] for k in tracks.ALL)
        self.assertAlmostEqual(total, 196349.0, places=0)

    def test_持股比例偏離帶納入建議(self):
        e = self.env()
        self.assertEqual(e["core"]["ratio_band"]["action"], risk.REDUCE)

    def test_衛星軌冷卻中不可新倉但可停損(self):
        """停損是出場，冷卻只擋新倉——冷卻中還不准停損等於把虧損部位鎖死"""
        e = self.env(exits=[{"date": "2026-08-10", "stopped": True}] * 3)
        self.assertTrue(e["tracks"]["satellite"]["halted"])
        self.assertTrue(e["can_sell"])

    def test_envelope帶入限額供下游檢查(self):
        limits = self.env()["limits"]
        self.assertEqual(limits["per_order_cap"], risk.DEFAULTS["per_order_cap"])
        self.assertEqual(limits["min_trade_value"], risk.DEFAULTS["min_trade_value"])

    def test_記錄當日原始檔數供日後遲滯計算(self):
        """遲滯要比對歷次的『公式原值』，envelope 不留它就沒得比"""
        self.assertEqual(self.env()["core"]["raw_max_names"], 2)

    def test_執行邊界帶出檔數上限的性質(self):
        """決策層要從 envelope 知道 max_names 是硬上限還是參考值"""
        self.assertEqual(self.env()["core"]["names_cap"], "hard")      # packet 未標時預設硬規則
        pk = make_packet()
        pk["discipline"]["names_cap"] = "advisory"
        self.assertEqual(self.env(packet=pk)["core"]["names_cap"], "advisory")

    def test_檔數下限是設定不是震盪_不受遲滯壓制(self):
        """歷史全是 1、今天 packet 因 ARK_NAMES_FLOOR 算出 3：遲滯會把有效值留在 1，
        但下限是使用者設定而非總資源在門檻附近震盪，envelope 要立刻放行 3"""
        pk = make_packet()
        pk["discipline"] = {**pk["discipline"], "max_names": 3, "names_floor": 3}
        e = risk.build_envelope(
            packet=pk, assigned=ASSIGNED,
            equity_points=[equity.make_point("2026-08-10", 196349.0, 14673.0, 47600.0)],
            satellite_exits=[], calendar=CALENDAR, today="2026-08-11",
            effective_max_names=1, max_names_history=[1, 1, 1, 1, 3],
            config=risk.DEFAULTS)
        self.assertEqual(e["core"]["max_names"], 3)

    def test_衛星軌配額與閘門進envelope(self):
        pk = make_packet(suggested_ratio=70.0, stock_value=180000.0, cash=20000.0,
                         industry={"source": "api/st/10",
                                   "candidates": {"6274": {"sector": "PCB-製造", "heating": False}},
                                   "sectors": {"2330": "半導體", "6274": "PCB-製造"},
                                   "error": None})
        e = risk.build_envelope(
            packet=pk, assigned=ASSIGNED, equity_points=[], satellite_exits=[],
            calendar=CALENDAR, today="2026-08-11", effective_max_names=2,
            max_names_history=[2], config=risk.DEFAULTS,
            gates={**gates.DEFAULTS, "history": [1]})
        s = e["tracks"][tracks.SATELLITE]
        self.assertAlmostEqual(s["quota"], 200000.0 * 0.70 * 0.5)
        self.assertEqual(s["allowlist"], ["2330", "6274"])
        sat = e["satellite"]
        # envelope 只留五個閘門值本身——不能把 gates.json 的 history 一起存進每天的
        # envelope，那會讓存檔越滾越大
        self.assertNotIn("history", sat["gates"])
        self.assertEqual(sat["gates"], gates.DEFAULTS)
        self.assertAlmostEqual(sat["name_cap_value"], 24000.0)
        self.assertAlmostEqual(sat["sector_cap_value"], 50000.0)
        self.assertEqual(sat["sectors"]["2330"], "半導體")
        self.assertEqual(sat["sector_exposure"], {"半導體": 47600.0})
        self.assertEqual(sat["candidates_source"], "api/st/10")
        self.assertEqual(sat["stop_loss"], -0.12)
        self.assertEqual([c["code"] for c in sat["trim_candidates"]], ["2330"])   # 47,600 / 200,000 = 23.8%

    def test_沒有gates時沿用舊配額與停損(self):
        e = self.env()
        s = e["tracks"][tracks.SATELLITE]
        self.assertAlmostEqual(s["quota"], risk.total_resource(make_packet()) * 0.25)
        self.assertEqual(e["satellite"]["stop_loss"], risk.DEFAULTS["satellite_stop_loss"])


class TestFullyBlocked(unittest.TestCase):
    """排程要能分辨「今天不該交易」與「程式壞了」——前者離開碼 3、後者 2。
    只擋買（L1）不算全停：還能賣就仍該讓 Agent 跑一輪。"""

    def env(self, can_buy, can_sell):
        return {"can_buy": can_buy, "can_sell": can_sell}

    def test_完全不能動才算全停(self):
        self.assertTrue(risk.is_fully_blocked(self.env(False, False)))

    def test_只擋買不算全停(self):
        self.assertFalse(risk.is_fully_blocked(self.env(False, True)))

    def test_只擋賣不算全停(self):
        self.assertFalse(risk.is_fully_blocked(self.env(True, False)))

    def test_暢通不算全停(self):
        self.assertFalse(risk.is_fully_blocked(self.env(True, True)))


class TestEnvelopeHistory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def env(self, date, raw, effective):
        return {"schema": 1, "date": date,
                "core": {"raw_max_names": raw, "max_names": effective}}

    def test_無歷史時回空與None(self):
        self.assertEqual(risk.max_names_history(self.dir), ([], None))

    def test_依日期排序取出原值序列與最後生效值(self):
        for e in (self.env("2026-08-12", 2, 1), self.env("2026-08-10", 1, 1),
                  self.env("2026-08-11", 2, 1)):
            risk.save_envelope(e, self.dir)
        self.assertEqual(risk.max_names_history(self.dir), ([1, 2, 2], 1))

    def test_略過缺欄位的舊檔(self):
        risk.save_envelope({"schema": 1, "date": "2026-08-10", "core": {}}, self.dir)
        risk.save_envelope(self.env("2026-08-11", 2, 1), self.dir)
        self.assertEqual(risk.max_names_history(self.dir), ([2], 1))


class TestGatesFallback(unittest.TestCase):
    """gates.json 壞掉時風控層要退回預設並留下警告，而不是整條決策軌炸掉。"""

    def write(self, data):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "gates.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return p

    def test_區間外的閘門檔退回預設並帶警告(self):
        g, warning = risk.load_gates_or_defaults(self.write({"name_cap": 0.30}))
        self.assertEqual(g, gates.DEFAULTS)
        self.assertIn("gates.json 無效", warning)
        self.assertIn("name_cap", warning)

    def test_合法的閘門檔照讀且無警告(self):
        g, warning = risk.load_gates_or_defaults(self.write({"name_cap": 0.10}))
        self.assertIsNone(warning)
        self.assertEqual(g["name_cap"], 0.10)
        self.assertEqual(g["sector_cap"], gates.DEFAULTS["sector_cap"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 個股桶燈號（使用者 2026-10-08 明示）

LAMP_INDUSTRY = {"source": "api/st/10", "candidates": {"2330": {"heating": False}},
                 "value_codes": ["2330"], "heating_codes": ["6274"],
                 "sectors": {}, "error": None}


def lamp_row(tiers, suggest_qty=0):
    return {"tiers": list(tiers), "suggest_qty": suggest_qty}


class TestLampState(unittest.TestCase):
    """確認無燈號＝持股列有讀到且無位階、價值區與升溫區名單都完整且都不含它。
    任何一個來源缺失或讀取失敗都是未知——缺值不等於無燈號。"""

    def state(self, code, holdings, industry=LAMP_INDUSTRY):
        return risk.lamp_state(code, holdings, industry)

    def test_兩來源皆無燈號_確認無燈號(self):
        s = self.state("2383", {"2383": lamp_row([])})
        self.assertEqual(s["state"], risk.LAMP_NONE)
        self.assertIs(s["value"], False)
        self.assertIs(s["heating"], False)

    def test_只有價值(self):
        s = self.state("2330", {"2330": lamp_row(["價值"])})
        self.assertEqual(s["state"], risk.LAMP_VALUE_ONLY)

    def test_價值加升溫(self):
        ind = {**LAMP_INDUSTRY, "heating_codes": ["2330"]}
        s = self.state("2330", {"2330": lamp_row(["價值", "升溫"])}, ind)
        self.assertEqual(s["state"], risk.LAMP_VALUE_HEATING)

    def test_只有升溫(self):
        s = self.state("6274", {"6274": lamp_row(["升溫"])})
        self.assertEqual(s["state"], risk.LAMP_HEATING_ONLY)

    def test_來源矛盾時以亮燈的一方為準_不判無燈號(self):
        s = self.state("2330", {"2330": lamp_row([])})        # 持股列空、名單卻有
        self.assertEqual(s["state"], risk.LAMP_VALUE_ONLY)

    def test_升溫名單沒保存_未知(self):
        ind = {k: v for k, v in LAMP_INDUSTRY.items() if k != "heating_codes"}
        s = self.state("2383", {"2383": lamp_row([])}, ind)
        self.assertEqual(s["state"], risk.LAMP_UNKNOWN)

    def test_產業名單讀取失敗_未知(self):
        ind = {"source": None, "candidates": {}, "value_codes": None,
               "heating_codes": None, "sectors": {}, "error": "連不上"}
        s = self.state("2383", {"2383": lamp_row([])}, ind)
        self.assertEqual(s["state"], risk.LAMP_UNKNOWN)

    def test_持股列沒讀到_未知(self):
        self.assertEqual(self.state("2383", {})["state"], risk.LAMP_UNKNOWN)

    def test_位階與建議調節整組缺失_未知(self):
        s = self.state("2383", {"2383": lamp_row([], suggest_qty=None)})
        self.assertEqual(s["state"], risk.LAMP_UNKNOWN)

    def test_10月8日川湖的舊決策包_升溫名單未存_未知(self):
        old = {"source": "api/st/10", "candidates": {}, "sectors": {}, "error": None}
        s = self.state("2059", {"2059": lamp_row([])}, old)
        self.assertEqual(s["state"], risk.LAMP_UNKNOWN)


NONE_INDUSTRY = {**LAMP_INDUSTRY, "candidates": {}, "value_codes": []}


class TestLampExitsInEnvelope(unittest.TestCase):
    """確認無燈號的個股桶部位，在使用者設定的範圍內列入必賣；未知不得誤觸發。"""

    def env(self, holdings, industry, scope="all"):
        p = make_packet(industry=industry)
        p["ark"]["holdings"] = holdings
        p["generated_at"] = "2026-08-11T10:01:00"
        return TestBuildEnvelope.env(self, packet=p, config={"lamp_exit_scope": scope})

    def test_預設範圍關閉_確認無燈號也不強制(self):
        e = self.env({"2330": lamp_row([])}, NONE_INDUSTRY, scope=risk.DEFAULTS["lamp_exit_scope"])
        self.assertEqual(e["satellite"]["lamp_exits"], [])
        self.assertEqual(e["satellite"]["lamps"]["2330"]["state"], risk.LAMP_NONE)   # 燈號仍算出

    def test_範圍限定代號_只列清單內(self):
        e = self.env({"2330": lamp_row([])}, NONE_INDUSTRY, scope=["2059"])
        self.assertEqual(e["satellite"]["lamp_exits"], [])
        e = self.env({"2330": lamp_row([])}, NONE_INDUSTRY, scope=["2330"])
        self.assertEqual(e["satellite"]["lamp_exits"], [{"code": "2330", "qty": 20}])

    def test_確認無燈號_列入必賣且整筆(self):
        e = self.env({"2330": lamp_row([])}, NONE_INDUSTRY)
        self.assertEqual(e["satellite"]["lamp_exits"], [{"code": "2330", "qty": 20}])
        self.assertEqual(e["satellite"]["lamps"]["2330"]["state"], risk.LAMP_NONE)
        self.assertEqual(e["satellite"]["lamps_as_of"], "2026-08-11T10:01:00")

    def test_未知不列入必賣(self):
        e = self.env({"2330": lamp_row([])},
                     {"source": None, "candidates": {}, "value_codes": None,
                      "heating_codes": None, "sectors": {}, "error": "連不上"})
        self.assertEqual(e["satellite"]["lamp_exits"], [])
        self.assertEqual(e["satellite"]["lamps"]["2330"]["state"], risk.LAMP_UNKNOWN)

    def test_只有價值不列入必賣(self):
        e = self.env({"2330": lamp_row(["價值"])}, LAMP_INDUSTRY)
        self.assertEqual(e["satellite"]["lamp_exits"], [])

    def test_非個股桶的部位不判燈號(self):
        e = self.env({"2308": lamp_row([]), "2330": lamp_row(["價值"])}, LAMP_INDUSTRY)
        self.assertNotIn("2308", e["satellite"]["lamps"])       # 2308 歸繼承軌


class TestLampExitScopeFromEnv(unittest.TestCase):
    def test_未設定或off為關閉(self):
        self.assertEqual(risk.lamp_exit_scope_from_env({}), "off")
        self.assertEqual(risk.lamp_exit_scope_from_env({"ARK_LAMP_EXIT": "OFF"}), "off")

    def test_all為整個個股桶(self):
        self.assertEqual(risk.lamp_exit_scope_from_env({"ARK_LAMP_EXIT": "all"}), "all")

    def test_代號清單(self):
        self.assertEqual(risk.lamp_exit_scope_from_env({"ARK_LAMP_EXIT": " 2059, 2383 "}),
                         ["2059", "2383"])


class TestLampStateMalformedRow(unittest.TestCase):
    """持股列的位階欄缺失或型別不對＝沒讀到，不是空燈號。兩份名單完整且都不含它時，
    只有 tiers=[]（真的讀到、真的沒字樣）才能當確認無燈號的證據。"""

    def state(self, holding):
        return risk.lamp_state("2383", {"2383": holding}, LAMP_INDUSTRY)["state"]

    def test_tiers為空清單且名單完整_確認無燈號(self):
        self.assertEqual(self.state({"tiers": [], "suggest_qty": 0}), risk.LAMP_NONE)

    def test_tiers缺欄_未知(self):
        self.assertEqual(self.state({"suggest_qty": 0}), risk.LAMP_UNKNOWN)

    def test_tiers為None_未知(self):
        self.assertEqual(self.state({"tiers": None, "suggest_qty": 0}), risk.LAMP_UNKNOWN)

    def test_tiers型別不對_未知(self):
        for bad in ("", "價值", {"價值": True}, 0, [None], [1]):
            with self.subTest(tiers=bad):
                self.assertEqual(self.state({"tiers": bad, "suggest_qty": 0}), risk.LAMP_UNKNOWN)

    def test_suggest_qty型別不對_未知(self):
        for bad in ("0", 0.5, True, [0]):
            with self.subTest(suggest_qty=bad):
                self.assertEqual(self.state({"tiers": [], "suggest_qty": bad}), risk.LAMP_UNKNOWN)

    def test_持股列本身不是物件_未知(self):
        for bad in (None, [], "2383"):
            with self.subTest(holding=bad):
                self.assertEqual(self.state(bad), risk.LAMP_UNKNOWN)

    def test_名單型別不對_未知(self):
        ind = {**LAMP_INDUSTRY, "heating_codes": "6274"}
        s = risk.lamp_state("2383", {"2383": {"tiers": [], "suggest_qty": 0}}, ind)
        self.assertEqual(s["state"], risk.LAMP_UNKNOWN)
