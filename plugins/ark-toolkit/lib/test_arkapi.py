"""arkapi 的純邏輯測試（不連網、不需要 ARK 執行中，任何平台可跑）

執行：cd plugin/plugins/ark-toolkit/lib && uv run --no-project --python 3.13 python -m unittest test_arkapi -v
"""
import base64
import datetime as dt
import json
import os
import unittest

import arkapi


def jwt(exp):
    """組一個只有 exp／jti 的假 JWT（與 ARK 實際簽發的形狀相同，簽章不驗）"""
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'HS512'})}.{seg({'exp': int(exp.timestamp()), 'jti': 'x'})}.sig"


NOW = dt.datetime(2026, 9, 2, 20, 0)


class TestTokenExpiry(unittest.TestCase):
    """token 會過期（實測壽命從數天到數週）。過期就必須停手——拿過期的 token 去打
    只會拿到 401，而呼叫端多半會把它誤讀成「這個欄位沒有資料」。"""

    def test_讀得出到期時間(self):
        exp = NOW + dt.timedelta(days=1)
        self.assertEqual(arkapi.token_expiry(jwt(exp)), exp.replace(microsecond=0))

    def test_過期的_token_視為無效(self):
        self.assertFalse(arkapi.token_valid(jwt(NOW - dt.timedelta(minutes=1)), now=NOW))

    def test_快到期的也視為無效_留安全邊際(self):
        # 剩不到 60 秒就別用了：一輪決策包要跑好幾分鐘，中途過期比一開始就失敗更難查
        self.assertFalse(arkapi.token_valid(jwt(NOW + dt.timedelta(seconds=30)), now=NOW))

    def test_還有效的可以用(self):
        self.assertTrue(arkapi.token_valid(jwt(NOW + dt.timedelta(hours=2)), now=NOW))

    def test_形狀不對的_token_不當成有效(self):
        self.assertFalse(arkapi.token_valid("not-a-jwt", now=NOW))


class TestRows(unittest.TestCase):
    """ARK 的回應一律是 {code, message, title, data}：title 是欄位名、data 是列。"""

    def test_title_與_data_併成_dict(self):
        payload = {"code": "0000", "title": ["股票代號", "總股數"],
                   "data": [["0050", "53"], ["0056", "106"]]}
        self.assertEqual(arkapi.rows(payload),
                         [{"股票代號": "0050", "總股數": "53"},
                          {"股票代號": "0056", "總股數": "106"}])

    def test_錯誤碼直接拋_不回半套資料(self):
        with self.assertRaises(arkapi.ArkApiError):
            arkapi.rows({"code": "9999", "message": "登入逾時"})

    def test_少欄位的列以_None_補齊_而不是靜默丟掉(self):
        payload = {"code": "0000", "title": ["a", "b", "c"], "data": [["1", "2"]]}
        self.assertEqual(arkapi.rows(payload), [{"a": "1", "b": "2", "c": None}])


class TestParsers(unittest.TestCase):
    def test_五指標與持股水位建議(self):
        payload = {"code": "0000",
                   "title": ["全球散戶", "外資情緒", "位階增溫", "社交反指", "量能暴衝", "持股水位建議"],
                   "data": [["45", "26.27000", "80.4036", "87.5288", "62.0681", "69.9"]]}
        got = arkapi.parse_indicators(payload)
        self.assertEqual(got["持股水位建議"], 69.9)
        self.assertEqual(got["全球散戶"], 45.0)

    def test_庫存只取台股_股數與均價轉數值(self):
        payload = {"code": "0000",
                   "title": ["股票代號", "種類", "總股數", "成交均價", "排序", "建立時間"],
                   "data": {"usStock": [], "twStock": [["0056", "1", "106.0", "53.6", "0", "t"],
                                                      ["0050", "1", "53.0", "106.28", "1", "t"]]}}
        self.assertEqual(arkapi.parse_positions(payload),
                         {"0056": (106, 53.6), "0050": (53, 106.28)})

    def test_現金台幣美元(self):
        payload = {"code": "0000", "title": ["台幣金額", "美元金額"], "data": ["75443", "0"]}
        self.assertEqual(arkapi.parse_cash(payload), (75443.0, 0.0))


ZONE_PAYLOAD = {
    "code": "0000", "message": "操作成功",
    "title": ["股票代號", "即時股價", "即時漲跌幅", "股票名稱", "(營收)單月營收變動%",
              "(營收)單月營收成長%", "(營收)累計月營收成長%", "(利潤)毛利率%",
              "(利潤)營業利益率%", "(利潤)稅後純益率%", "(負債)負債比率%", "(負債)流動比率%",
              "(負債)速動比率%", "(現金流)營業活動變動%", "(現金流)自由現金變動%",
              "連續配息", "產業別", "K(9)"],
    "data": [
        ["6274", "", "", "台燿", "0.11", "132.48", "97.01", "29.72", "23.48", "16.37",
         "48.55", "205.37", "149.06", "-54.41", "-159.09", "23", "PCB-製造", "21.2432"],
        ["2855", "", "", "統一證", "", "", "", "89.94", "48.68", "76.87",
         "87.34", "112.56", "108.65", "-1371.40", "39.79", "9", "金融-證券", "93.5475"],
    ],
}


class TestZone(unittest.TestCase):
    def test_產業分區列轉成代號索引的指標(self):
        z = arkapi.parse_zone(ZONE_PAYLOAD)
        self.assertEqual(set(z), {"6274", "2855"})
        self.assertEqual(z["6274"]["name"], "台燿")
        self.assertEqual(z["6274"]["sector"], "PCB-製造")
        self.assertAlmostEqual(z["6274"]["rev_yoy"], 132.48)
        self.assertAlmostEqual(z["6274"]["gross_margin"], 29.72)
        self.assertEqual(z["6274"]["dividend_years"], 23)
        self.assertAlmostEqual(z["6274"]["k9"], 21.2432)

    def test_空字串欄位是_None_不是零(self):
        # 金融股沒有營收欄，空字串必須保留為缺值，否則排序會把它當成營收零成長
        z = arkapi.parse_zone(ZONE_PAYLOAD)
        self.assertIsNone(z["2855"]["rev_yoy"])
        self.assertAlmostEqual(z["2855"]["net_margin"], 76.87)

    def test_zone_走白名單內的路徑(self):
        sess = arkapi.Session(token=jwt(NOW + dt.timedelta(days=1)), member_id="1" * 20,
                              device_token="dev", expires=NOW + dt.timedelta(days=1))
        seen = {}

        def fetch(url, headers, body):
            seen["url"] = url
            seen["body"] = body
            return ZONE_PAYLOAD
        z = arkapi.zone(sess, arkapi.INDUSTRY_VALUE, fetch=fetch, now=NOW)
        self.assertIn("/api/st/10?", seen["url"])
        # 這個端點對 GET 回 405，必須是 POST 空 JSON 物件
        self.assertEqual(seen["body"], {})
        self.assertIn("6274", z)

    def test_分區以外的_st_路徑仍被拒(self):
        sess = arkapi.Session(token=jwt(NOW + dt.timedelta(days=1)), member_id="1" * 20,
                              device_token="dev", expires=NOW + dt.timedelta(days=1))
        with self.assertRaises(ValueError):
            arkapi.call(sess, "api/st/10/delete", fetch=lambda *a, **k: {}, now=NOW)


class TestEndpointLabel(unittest.TestCase):
    """錯誤訊息只能露出端點名稱，不能露出查詢字串（deviceToken 在裡面）——
    packet.py 會把 str(e) 原封不動存進明文 packet JSON。"""

    def test_只取路徑最後一段_不含查詢字串裡的_deviceToken(self):
        url = "https://host/dashaWulinApi/api/st/10?deviceType=I&deviceToken=abc"
        label = arkapi._endpoint_label(url)
        self.assertEqual(label, "10")
        self.assertNotIn("deviceToken", label)
        self.assertNotIn("abc", label)


class TestReadOnlyGuard(unittest.TestCase):
    """這個模組是唯讀的。寫入端點存在且格式已知，但**不從這裡走**——
    要走得經過使用者同意、另外設計驗證（AX 寫入有 App 自算總成本當獨立見證，
    API 只回一句「操作成功」）。白名單讓「不小心打到寫入端點」變成不可能。"""

    def test_白名單外的路徑一律拒絕(self):
        sess = arkapi.Session("t", "m", "d", NOW + dt.timedelta(hours=1))
        for path in ("api/myStock/update/1/0050/1/10/100", "api/calculateRisk/memberMoney/update",
                     "api/dailyReward/upsert", "api/watchlistUpdate"):
            with self.assertRaises(ValueError, msg=path):
                arkapi.call(sess, path, fetch=lambda *a, **k: {})

    def test_白名單內的路徑放行(self):
        token = jwt(NOW + dt.timedelta(hours=1))
        sess = arkapi.Session(token, "m", "d", NOW + dt.timedelta(hours=1))
        seen = {}

        def fetch(url, headers, body):
            seen["url"], seen["headers"] = url, headers
            return {"code": "0000", "title": ["a"], "data": [["1"]]}

        arkapi.call(sess, "api/calculateRisk", fetch=fetch, now=NOW)
        self.assertIn("api/calculateRisk?", seen["url"])
        self.assertEqual(seen["headers"]["Authorization"], f"Bearer {token}")

    def test_帶_memberId_的庫存路徑也在白名單內(self):
        token = jwt(NOW + dt.timedelta(hours=1))
        sess = arkapi.Session(token, "20260313220729978000", "d", NOW + dt.timedelta(hours=1))
        arkapi.call(sess, f"api/myStock/{sess.member_id}", body={},
                    fetch=lambda *a, **k: {"code": "0000"}, now=NOW)

    def test_token_過期就不送出請求(self):
        sess = arkapi.Session("t", "m", "d", NOW - dt.timedelta(hours=1))

        def fetch(*a, **k):
            raise AssertionError("過期還送出請求了")

        with self.assertRaises(arkapi.ArkApiError):
            arkapi.call(sess, "api/calculateRisk", fetch=fetch, now=NOW)


class TestNewReadOnlyEndpoints(unittest.TestCase):
    """1.9.3 新增的唯讀查詢。端點、body 與回應欄位都取自 App 自己呼叫過、留在 URL
    快取裡的紀錄（非猜測），這裡用假 fetch 驗證：路徑進得了白名單、body／query 組對、
    回應解析正確。寫入端點與 api/auth 刻意不收進唯讀層。"""

    def _sess(self):
        token = jwt(NOW + dt.timedelta(hours=1))
        return arkapi.Session(token, "20260313220729978000", "dev", NOW + dt.timedelta(hours=1))

    def _fetch(self, store):
        def fetch(url, headers, body):
            store["url"], store["body"] = url, body
            return store["resp"]
        return fetch

    def test_目前點數取當前點數欄位(self):
        s = {"resp": {"code": "0000",
                      "title": ["會員序號", "當前點數", "AuthType", "是否為教練 1:是 0:否"],
                      "data": [["x", "1,250", "1", "0"]]}}
        self.assertEqual(arkapi.points(self._sess(), fetch=self._fetch(s), now=NOW), 1250.0)
        self.assertIn("api/pointRecord/getCurrentPoint?", s["url"])
        self.assertIn("memberId=", s["url"])

    def test_匯率(self):
        s = {"resp": {"code": "0000", "title": ["匯率"], "data": [["32.15"]]}}
        self.assertEqual(arkapi.exchange_rate(self._sess(), fetch=self._fetch(s), now=NOW), 32.15)

    def test_每日資產帶日期區間與member_id(self):
        s = {"resp": {"code": "0000", "title": ["資料日期", "股票庫存總市值", "現金餘額"],
                      "data": [["20261007", "100", "5"], ["20261008", "110", "5"]]}}
        got = arkapi.asset_daily(self._sess(), "20261007", dt.date(2026, 10, 10),
                                 fetch=self._fetch(s), now=NOW)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["資料日期"], "20261007")
        self.assertEqual(s["body"]["start_date"], "20261007")
        self.assertEqual(s["body"]["end_date"], "20261010")       # date → YYYYMMDD
        self.assertEqual(s["body"]["member_id"], "20260313220729978000")

    def test_策略清單(self):
        s = {"resp": {"code": "0000", "title": ["策略ID", "策略名稱"],
                      "data": [["10", "產業價值區"], ["6", "產業升溫區"]]}}
        ids = arkapi.strategy_ids(self._sess(), fetch=self._fetch(s), now=NOW)
        self.assertEqual(ids[0]["策略名稱"], "產業價值區")

    def test_個股資料把symbols放進body(self):
        s = {"resp": {"code": "0000",
                      "title": ["股票代號", "K(9)", "淨值", "折溢價", "成交量", "成交金額"],
                      "data": [["00631L", "85", "1.2", "0.1", "100", "200"]]}}
        got = arkapi.stock_data(self._sess(), ["00631L", "0050"], fetch=self._fetch(s), now=NOW)
        self.assertEqual(got[0]["股票代號"], "00631L")
        self.assertEqual(s["body"]["symbols"], ["00631L", "0050"])

    def test_auth端點不在白名單_唯讀層不自行換發token(self):
        with self.assertRaises(ValueError):
            arkapi.call(self._sess(), "api/auth", body={}, fetch=lambda *a, **k: {}, now=NOW)


class TestWriteLayer(unittest.TestCase):
    """寫入層（1.9.3 mitm 實測格式）：雙開關守衛、path／body 組法、read-after-write。
    全程用假 fetch，不連網、不動任何帳戶。"""

    def setUp(self):
        self._prev = os.environ.get(arkapi.WRITES_ENABLED_ENV)
        os.environ[arkapi.WRITES_ENABLED_ENV] = "1"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(arkapi.WRITES_ENABLED_ENV, None)
        else:
            os.environ[arkapi.WRITES_ENABLED_ENV] = self._prev

    def _sess(self):
        token = jwt(NOW + dt.timedelta(hours=1))
        return arkapi.Session(token, "20260313220729978000", "dev", NOW + dt.timedelta(hours=1))

    def _fetch(self, *, positions=None, cash=(0.0, 0.0), sent=None):
        positions = positions or {}

        def fetch(url, headers, body):
            if sent is not None:
                sent.append((url, body))
            if "memberMoney/update" in url:
                return {"code": "0000", "message": "操作成功"}
            if "calculateRisk/memberMoney" in url:                      # 現金讀回
                return {"code": "0000", "title": ["台幣金額", "美元金額"],
                        "data": [str(cash[0]), str(cash[1])]}
            if ("/myStock/insert" in url or "/myStock/update/" in url
                    or "avgPrice/upsert" in url or "/myStock/delete/" in url):
                return {"code": "0000", "message": "操作成功"}
            if "/api/myStock/" in url:                                  # 庫存讀回
                tw = [[c, "1", str(float(q)), str(p), "0", "t"] for c, (q, p) in positions.items()]
                return {"code": "0000",
                        "title": ["股票代號", "種類", "總股數", "成交均價", "排序", "建立時間"],
                        "data": {"twStock": tw, "usStock": []}}
            return {"code": "0000"}
        return fetch

    def test_寫入預設關閉_需旗標與confirm(self):
        sess = self._sess()
        os.environ.pop(arkapi.WRITES_ENABLED_ENV, None)                 # 沒有旗標
        with self.assertRaises(arkapi.WritesDisabled):
            arkapi.set_cash(sess, 5000, usd=0, confirm=True, fetch=self._fetch(), now=NOW)
        os.environ[arkapi.WRITES_ENABLED_ENV] = "1"                     # 有旗標但沒 confirm
        with self.assertRaises(arkapi.WritesDisabled):
            arkapi.set_cash(sess, 5000, usd=0, confirm=False, fetch=self._fetch(), now=NOW)

    def test_set_cash_送memberMoney_update並讀回驗證(self):
        sent = []
        f = self._fetch(cash=(5000.0, 0.0), sent=sent)
        arkapi.set_cash(self._sess(), 5000, usd=0, confirm=True, fetch=f, now=NOW)
        upd = next(b for u, b in sent if "memberMoney/update" in u)
        self.assertEqual(upd["amount_twd"], "5000")
        self.assertEqual(upd["member_id"], "20260313220729978000")

    def test_set_cash_省略usd時保留原本美元(self):
        sent = []
        f = self._fetch(cash=(5000.0, 123.0), sent=sent)               # 現有美元 123
        arkapi.set_cash(self._sess(), 5000, confirm=True, fetch=f, now=NOW)  # usd 省略
        upd = next(b for u, b in sent if "memberMoney/update" in u)
        self.assertEqual(upd["amount_usd"], "123")                     # 不被歸零

    def test_insert_holding_body與驗證(self):
        sent = []
        f = self._fetch(positions={"2313": (1, 200.0)}, sent=sent)
        arkapi.insert_holding(self._sess(), "2313", 1, 200, confirm=True, fetch=f, now=NOW)
        ins = next(b for u, b in sent if "/myStock/insert" in u)
        self.assertEqual(ins["stock_code"], "2313")
        self.assertEqual(ins["totle_count"], "1.0")
        self.assertEqual(ins["avg_price"], "200")
        self.assertEqual((ins["type"], ins["market"]), ("1", "1"))

    def test_update_holding_走path參數並打avgPrice_upsert(self):
        sent = []
        f = self._fetch(positions={"00935": (33, 62.76)}, sent=sent)
        arkapi.update_holding(self._sess(), "00935", 33, 62.76, confirm=True, fetch=f, now=NOW)
        urls = [u for u, _ in sent]
        self.assertTrue(any("/myStock/update/20260313220729978000/00935/1/33.0/62.76" in u for u in urls))
        self.assertTrue(any("avgPrice/upsert" in u for u in urls))

    def test_delete_holding_走path並驗證消失(self):
        sent = []
        f = self._fetch(positions={}, sent=sent)                       # 刪後讀回已無
        arkapi.delete_holding(self._sess(), "2313", confirm=True, fetch=f, now=NOW)
        self.assertTrue(any("/myStock/delete/20260313220729978000/2313/1" in u for u, _ in sent))

    def test_寫入後讀回不符就拋(self):
        f = self._fetch(positions={})                                  # insert 後卻讀不到該檔
        with self.assertRaises(arkapi.ArkApiError):
            arkapi.insert_holding(self._sess(), "2313", 1, 200, confirm=True, fetch=f, now=NOW)


if __name__ == "__main__":
    unittest.main()
