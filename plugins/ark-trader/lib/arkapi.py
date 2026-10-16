"""ARK 伺服器的**唯讀**查詢層。

為什麼要有這一層：有些數字 UI 根本讀不到。運算頁的五指標與「持股水位建議」是
純圖形儀表板，AX tree 裡沒有任何文字；而伺服器的 `api/calculateRisk` 直接回數值。
另外庫存可以在這裡再讀一次，和 AX 解析出來的對帳——parser 被 App 改版弄壞時，
兩邊不一致會當場現形，而不是靜默讀成 0 檔。

**這個模組不寫入，也不該被改成會寫入。** 寫入端點存在、格式也已知，但走 API 寫入
是另一個決定：AX 寫入有 App 自算的總成本當獨立見證，API 只回一句「操作成功」，
而且繞過 UI 寫入後 App 端的建議是否跟著刷新並未驗證。`call()` 的白名單讓「不小心
打到寫入端點」變成不可能，而不只是「我們沒有呼叫它」。

**認證**：重用 App 自己拿到的 Bearer token（從它的 URL 快取裡讀），不自行向
`api/auth` 換發。App 沒登入、或 token 過期，這裡就明確失敗——那代表該去把 App
打開，而不是繞過它。token 只在記憶體裡流動，不寫檔、不記錄。
"""
import base64
import datetime as dt
import glob
import json
import os
import plistlib
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple, Optional

BASE = "https://ark-api.my-galaxy.com.tw/dashaWulinApi/"
PREFS_GLOB = "~/Library/Containers/*/Data/Library/Preferences/com.galaxy.ark.plist"
CACHE_REL = "Data/Library/Caches/com.galaxy.ark/Cache.db"

# 只有這些路徑送得出去。全部是查詢；任何會改變伺服器狀態的端點都不在此列。
READ_ONLY = {
    "api/calculateRisk",                 # 五指標 + 持股水位建議（AX 讀不到的儀表板）
    "api/calculateRisk/memberMoney",     # 台幣／美元現金
    "api/myStock/{member_id}",           # 庫存
    "api/myStock/assetDaily",            # 每日總資產序列（庫存市值＋現金餘額）
    "api/pointRecord/getCurrentPoint",   # 目前點數（教練／會員）
    "api/stockData",                     # 一批代號的盤中資料（K(9)／淨值／折溢價…）
    "api/stockData/exchangeRate",        # 美元兌台幣匯率
    "api/commonConfig/list",             # App 共用設定清單
    "api/st/ids",                        # 策略分區清單
    "api/st/{zone_id}",                  # 策略分區內容（產業價值區 10、產業升溫區 6…）
}
# 上面 assetDaily／getCurrentPoint／stockData／exchangeRate／commonConfig 五個端點與其
# 回應欄位，取自 App 1.9.3 自己呼叫過、留在 URL 快取裡的紀錄（非猜測）。api/auth（換發
# token，body 帶 appId／appSecret）刻意不列——見模組開頭「認證」段：唯讀層不自行換發。
EXPIRY_MARGIN = dt.timedelta(seconds=60)   # 剩這麼點時間就別開始了


class ArkApiError(RuntimeError):
    pass


class Session(NamedTuple):
    token: str
    member_id: str
    device_token: str
    expires: dt.datetime


# --- token ------------------------------------------------------------------

def token_expiry(token) -> Optional[dt.datetime]:
    """JWT 的 exp（本地時間）；形狀不對回 None。簽章不驗——我們不是發證方，
    只是要知道手上這張還能不能用。"""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return dt.datetime.fromtimestamp(claims["exp"])
    except Exception:                    # noqa: BLE001 - 任何解析失敗都只代表「不能用」
        return None


def token_valid(token, now=None) -> bool:
    exp = token_expiry(token)
    return exp is not None and exp - EXPIRY_MARGIN > (now or dt.datetime.now())


# --- 從 App 的本機檔案組出 session -------------------------------------------

def container():
    """ARK 的容器目錄。目錄名是每台機器不同的 UUID，只能由偏好設定檔反查——
    寫死 UUID 的話換一台機器就整個模組失效。"""
    hits = glob.glob(os.path.expanduser(PREFS_GLOB))
    if not hits:
        raise ArkApiError("找不到方舟運算的容器（App 可能沒安裝或從未開啟過）")
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(hits[0]))))


def _cached_credentials(cache_db):
    """從 App 的 URL 快取撈 (最新且未過期的 token, member_id)。

    先複製再讀：App 隨時在寫這個 SQLite，直接開它可能讀到半截交易，
    也可能干擾 App。WAL 與 SHM 要一起複製，否則看不到最近的寫入。
    """
    tmp = tempfile.mkdtemp(prefix="arkapi-")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = cache_db + suffix
            if os.path.exists(src):
                shutil.copy(src, os.path.join(tmp, "Cache.db" + suffix))
        db = sqlite3.connect(os.path.join(tmp, "Cache.db"))
        rows = db.execute(
            "select r.request_key, b.request_object "
            "from cfurl_cache_response r "
            "join cfurl_cache_blob_data b on r.entry_ID = b.entry_ID "
            "where r.request_key like '%dashaWulinApi%' "
            "order by r.time_stamp desc limit 200").fetchall()
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    member_id, best = None, None
    for key, blob in rows:
        if member_id is None and (m := re.search(r"/(\d{18,22})", key)):
            member_id = m.group(1)
        try:
            headers = next(x for x in plistlib.loads(blob)["Array"]
                           if isinstance(x, dict) and "Authorization" in x)
        except Exception:                         # noqa: BLE001 - 壞掉的那筆跳過就好
            continue
        token = headers["Authorization"].split(" ", 1)[-1]
        exp = token_expiry(token)
        if exp and (best is None or exp > best[1]):
            best = (token, exp)
    return best, member_id


def session(now=None) -> Session:
    """重用 App 自己的登入。App 沒登入過、或最新的 token 已過期，就明確失敗。"""
    root = container()
    prefs_path = os.path.join(root, "Data/Library/Preferences/com.galaxy.ark.plist")
    with open(prefs_path, "rb") as f:
        prefs = plistlib.load(f)
    device_token = prefs.get("deviceToken")
    if not device_token:
        raise ArkApiError("方舟運算的偏好設定裡沒有 deviceToken，App 可能還沒登入過")

    cache_db = os.path.join(root, CACHE_REL)
    if not os.path.exists(cache_db):
        raise ArkApiError("找不到方舟運算的 URL 快取，無法取得登入憑證")
    best, member_id = _cached_credentials(cache_db)
    if best is None or member_id is None:
        raise ArkApiError("快取裡沒有可用的登入憑證，請先開啟方舟運算並登入")
    token, exp = best
    if not token_valid(token, now=now):
        raise ArkApiError(f"快取裡的登入憑證已於 {exp:%Y-%m-%d %H:%M} 過期，"
                          "請開啟方舟運算讓它重新登入")
    return Session(token=token, member_id=member_id,
                   device_token=device_token, expires=exp)


# --- 呼叫 -------------------------------------------------------------------

def _endpoint_label(url):
    """錯誤訊息只能露出端點名稱——URL 的查詢字串裡有 deviceToken，
    不能整條塞進 ArkApiError（packet.py 會把訊息原封不動存進明文 packet JSON）。"""
    return urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]


def _fetch(url, headers, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ArkApiError(f"{_endpoint_label(url)} 回應 HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ArkApiError(f"連不上 ARK 伺服器：{e.reason}") from e


def call(sess, path, body=None, query=None, fetch=_fetch, now=None):
    """送出一次**查詢**。path 必須在 READ_ONLY 白名單裡，否則拒絕。"""
    template = re.sub(r"/\d{18,22}(?=/|$)", "/{member_id}", path)
    template = re.sub(r"^api/st/\d{1,3}$", "api/st/{zone_id}", template)
    if template not in READ_ONLY:
        raise ValueError(f"arkapi 是唯讀的，不送 {path}（白名單：{sorted(READ_ONLY)}）")
    if not token_valid(sess.token, now=now):
        raise ArkApiError(f"登入憑證已於 {sess.expires:%Y-%m-%d %H:%M} 過期，"
                          "請開啟方舟運算讓它重新登入")
    params = {"deviceType": "I", "deviceToken": sess.device_token, **(query or {})}
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {sess.token}", "Accept": "*/*"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return fetch(url, headers, body)


def rows(payload):
    """{code, message, title, data} → [{欄位名: 值}]。列比 title 短就補 None，
    不靜默丟掉——欄位變動要看得出來，這是 parser 壞掉時的第一個線索。"""
    if payload.get("code") != "0000":
        raise ArkApiError(f"ARK 回應 code={payload.get('code')}：{payload.get('message')}")
    title = payload.get("title") or []
    return [{name: row[i] if i < len(row) else None for i, name in enumerate(title)}
            for row in payload.get("data") or []]


def _num(v):
    return float(str(v).replace(",", "")) if v not in (None, "") else 0.0


def parse_indicators(payload):
    """五指標＋持股水位建議 → {名稱: 數值}。這些在 AX tree 裡完全讀不到。"""
    got = rows(payload)
    return {k: _num(v) for k, v in got[0].items()} if got else {}


def parse_positions(payload):
    """庫存 → {代號: (股數, 成交均價)}。只取台股；美股另有帳戶模型，這裡不混。"""
    if payload.get("code") != "0000":
        raise ArkApiError(f"ARK 回應 code={payload.get('code')}：{payload.get('message')}")
    title = payload.get("title") or []
    tw = (payload.get("data") or {}).get("twStock") or []
    idx = {name: i for i, name in enumerate(title)}
    return {row[idx["股票代號"]]: (int(_num(row[idx["總股數"]])), _num(row[idx["成交均價"]]))
            for row in tw}


def parse_cash(payload):
    """現金 → (台幣, 美元)。這個端點的 data 是單層陣列，不是列的陣列。"""
    if payload.get("code") != "0000":
        raise ArkApiError(f"ARK 回應 code={payload.get('code')}：{payload.get('message')}")
    data = payload.get("data") or []
    return (_num(data[0] if len(data) > 0 else 0), _num(data[1] if len(data) > 1 else 0))


# --- 對外的查詢 --------------------------------------------------------------

def indicators(sess, **kw):
    return parse_indicators(call(sess, "api/calculateRisk",
                                 body={"deviceType": "I", "deviceToken": sess.device_token}, **kw))


def cash(sess, **kw):
    return parse_cash(call(sess, "api/calculateRisk/memberMoney",
                           query={"memberId": sess.member_id}, **kw))


def positions(sess, **kw):
    return parse_positions(call(sess, f"api/myStock/{sess.member_id}", body={}, **kw))


INDUSTRY_VALUE = "10"      # api/st/ids 回的策略 ID：產業價值區
INDUSTRY_HEATING = "6"     # 產業升溫區

_ZONE_FIELDS = {
    "股票名稱": ("name", str),
    "產業別": ("sector", str),
    "(營收)單月營收變動%": ("rev_mom", float),
    "(營收)單月營收成長%": ("rev_yoy", float),
    "(營收)累計月營收成長%": ("rev_ytd", float),
    "(利潤)毛利率%": ("gross_margin", float),
    "(利潤)營業利益率%": ("op_margin", float),
    "(利潤)稅後純益率%": ("net_margin", float),
    "(負債)負債比率%": ("debt_ratio", float),
    "連續配息": ("dividend_years", int),
    "K(9)": ("k9", float),
}


def _opt(v, typ):
    """空字串是缺值（金融股沒有營收欄），不能變成 0——排序會把它當零成長。"""
    if v in (None, ""):
        return None
    return typ(_num(v)) if typ is not str else str(v)


def parse_zone(payload):
    """策略分區（產業價值區／升溫區）→ {代號: 指標}。欄位名對不上就缺欄，不猜。"""
    out = {}
    for row in rows(payload):
        code = row.get("股票代號")
        if not code:
            continue
        out[code] = {key: _opt(row.get(col), typ) for col, (key, typ) in _ZONE_FIELDS.items()}
    return out


def zone(sess, zone_id, body=None, **kw):
    # 伺服器對這個端點只接受 POST，空 body 即可；GET 回 405
    return parse_zone(call(sess, f"api/st/{zone_id}", body={} if body is None else body, **kw))


# --- 1.9.3 新增的唯讀查詢（端點、body、回應欄位均取自 App 自己的快取紀錄）---------

def _base_body(sess):
    """多數 POST 查詢共同的 body；App 實測每筆都帶這兩個欄位。"""
    return {"deviceType": "I", "deviceToken": sess.device_token}


def _ymd(d):
    """日期正規化成 ARK 用的 'YYYYMMDD'；已是字串就原樣送出（不臆測格式）。"""
    if isinstance(d, str):
        return d
    if isinstance(d, (dt.date, dt.datetime)):
        return d.strftime("%Y%m%d")
    raise ValueError(f"日期要給 'YYYYMMDD' 字串或 date，而不是 {type(d).__name__}")


def strategy_ids(sess, **kw):
    """策略分區清單：[{策略ID, 策略名稱}]。zone() 吃的 zone_id 就是這裡的策略ID；
    目前程式用的產業價值區／升溫區（INDUSTRY_VALUE／INDUSTRY_HEATING）也在這份清單裡。"""
    return rows(call(sess, "api/st/ids", body=_base_body(sess), **kw))


def points(sess, **kw):
    """目前點數。回應同列還有會員序號／AuthType／是否教練，需要時用 rows() 自取。"""
    got = rows(call(sess, "api/pointRecord/getCurrentPoint",
                    query={"memberId": sess.member_id}, **kw))
    return _num(got[0]["當前點數"]) if got else 0.0


def exchange_rate(sess, **kw):
    """美元兌台幣匯率——App 混合台／美股估值時用的那個數。"""
    got = rows(call(sess, "api/stockData/exchangeRate", body=_base_body(sess), **kw))
    return _num(got[0]["匯率"]) if got else 0.0


def asset_daily(sess, start, end, **kw):
    """每日總資產序列：[{資料日期, 股票庫存總市值, 現金餘額}]。值原樣保留字串，
    要畫圖或算報酬再由呼叫端轉數值。start／end 收 'YYYYMMDD' 或 date。"""
    body = {**_base_body(sess), "member_id": sess.member_id,
            "start_date": _ymd(start), "end_date": _ymd(end)}
    return rows(call(sess, "api/myStock/assetDaily", body=body, **kw))


def stock_data(sess, symbols, **kw):
    """一批代號的盤中資料：[{股票代號, K(9), 淨值, 折溢價, 成交量, 成交金額}]。
    symbols 是台股代號字串陣列（如 '00631L'）。"""
    body = {**_base_body(sess), "symbols": list(symbols)}
    return rows(call(sess, "api/stockData", body=body, **kw))


def common_config(sess, **kw):
    """App 共用設定清單。撰寫時未對照過實際回應欄位，故走通用 rows()：
    形狀不符會依 rows() 的規則明確失敗，而不是靜默吞掉。"""
    return rows(call(sess, "api/commonConfig/list", body=_base_body(sess), **kw))


# --- 寫入層（1.9.3 實測格式；預設關閉，無 checksum）---------------------------
#
# 設計見 references/write-api-conversion-assessment.md。四個前提：
#   1) 雙重開關：需 confirm=True 且環境變數 ARK_API_WRITES=1，否則連送都不送。
#   2) 無 checksum：mitm 實測寫入不帶任何簽章，Bearer token + deviceToken 即可。
#   3) read-after-write：寫完立刻用唯讀 API 讀回比對，不符即拋（取代 AX 的「總成本」見證）。
#   4) 只動台股（type/market 預設 "1"）；券商下單不在此層，維持 Shioaji。

WRITES_ENABLED_ENV = "ARK_API_WRITES"

# 僅供文件／測試對照：實測攔到的寫入端點模板（參數位置見註解）
WRITE_ENDPOINTS = {
    "api/myStock/insert",                                            # body: stock_code,type,market,totle_count,avg_price,member_id
    "api/myStock/update/{member_id}/{code}/{kind}/{qty}/{avgPrice}",  # 參數在 path，body {}
    "api/myStock/avgPrice/upsert",                                   # body: member_id,stock_code,data_date,avg_price
    "api/myStock/delete/{member_id}/{code}/{kind}",                  # 參數在 path
    "api/calculateRisk/memberMoney/update",                          # body: member_id,amount_twd,amount_usd
}


class WritesDisabled(PermissionError):
    pass


def _require_writes_enabled(confirm):
    if not confirm:
        raise WritesDisabled("寫入需 confirm=True（避免誤呼叫）")
    if os.environ.get(WRITES_ENABLED_ENV) != "1":
        raise WritesDisabled(f"寫入預設關閉：請設環境變數 {WRITES_ENABLED_ENV}=1 才會真的送出")


def _numstr(v):
    """ARK 寫入欄位要字串；整數不帶小數、其餘保留兩位。"""
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _qtystr(q):
    """股數：ARK path／body 用一位小數的浮點字串（實測如 '32.0'、'1.0'）。"""
    return str(float(q))


def _write_post(sess, path, body=None, query=None, fetch=_fetch, now=None):
    """送出一次寫入 POST（不經 READ_ONLY 白名單——寫入由各具名函式自己把關）。
    code 非 0000 即拋。呼叫端負責 read-after-write 驗證。"""
    if not token_valid(sess.token, now=now):
        raise ArkApiError(f"登入憑證已於 {sess.expires:%Y-%m-%d %H:%M} 過期，"
                          "請開啟方舟運算讓它重新登入")
    params = {"deviceType": "I", "deviceToken": sess.device_token, **(query or {})}
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {sess.token}", "Accept": "*/*",
               "Content-Type": "application/json"}
    resp = fetch(url, headers, body if body is not None else {})   # body None 也送 {} 以走 POST
    if resp.get("code") != "0000":
        raise ArkApiError(f"寫入未成功 code={resp.get('code')}：{resp.get('message')}")
    return resp


def set_cash(sess, twd, usd=None, *, confirm=False, verify=True, fetch=_fetch, now=None):
    """改運算頁現金。usd 省略時先讀回目前美元現金保留，避免被歸零（端點同時帶台幣與美元）。
    verify=True：寫入後讀回 cash() 比對台幣。"""
    _require_writes_enabled(confirm)
    if usd is None:
        _, usd = cash(sess, fetch=fetch, now=now)
    body = {**_base_body(sess), "member_id": sess.member_id,
            "amount_twd": str(int(round(twd))), "amount_usd": _numstr(usd)}
    resp = _write_post(sess, "api/calculateRisk/memberMoney/update", body=body, fetch=fetch, now=now)
    if verify:
        got_twd, _ = cash(sess, fetch=fetch, now=now)
        if abs(got_twd - round(twd)) > 0.5:
            raise ArkApiError(f"寫入後讀回台幣現金 {got_twd} 與目標 {round(twd)} 不符，視為未生效")
    return resp


def insert_holding(sess, code, qty, avg_price, *, kind="1", market="1",
                   confirm=False, verify=True, fetch=_fetch, now=None):
    """新增一檔台股庫存。code 已存在時 ARK 會回非 0000（請改用 update_holding）。"""
    _require_writes_enabled(confirm)
    body = {**_base_body(sess), "member_id": sess.member_id, "stock_code": code,
            "type": kind, "market": market, "totle_count": _qtystr(qty),
            "avg_price": _numstr(avg_price)}      # 注意 ARK 欄位拼法為 totle_count
    resp = _write_post(sess, "api/myStock/insert", body=body, fetch=fetch, now=now)
    if verify:
        pos = positions(sess, fetch=fetch, now=now)
        if code not in pos or pos[code][0] != int(float(qty)):
            raise ArkApiError(f"寫入後讀回未見 {code} 股數 {int(float(qty))}，視為未生效")
    return resp


def update_holding(sess, code, qty, avg_price, *, kind="1",
                   confirm=False, verify=True, fetch=_fetch, now=None):
    """改一檔台股的股數／均價。實測是兩支：update（path 參數、body {}）＋ avgPrice/upsert。"""
    _require_writes_enabled(confirm)
    path = f"api/myStock/update/{sess.member_id}/{code}/{kind}/{_qtystr(qty)}/{_numstr(avg_price)}"
    resp = _write_post(sess, path, body={}, fetch=fetch, now=now)
    _write_post(sess, "api/myStock/avgPrice/upsert",
                body={**_base_body(sess), "member_id": sess.member_id, "stock_code": code,
                      "data_date": dt.datetime.now().strftime("%Y%m%d"),
                      "avg_price": _numstr(avg_price)}, fetch=fetch, now=now)
    if verify:
        pos = positions(sess, fetch=fetch, now=now)
        got = pos.get(code)
        if not got or got[0] != int(float(qty)) or abs(got[1] - float(avg_price)) > 0.01:
            raise ArkApiError(f"寫入後讀回 {code}={got} 與目標 ({int(float(qty))}, {avg_price}) 不符")
    return resp


def delete_holding(sess, code, *, kind="1", confirm=False, verify=True, fetch=_fetch, now=None):
    """刪一檔台股庫存。參數在 path。"""
    _require_writes_enabled(confirm)
    resp = _write_post(sess, f"api/myStock/delete/{sess.member_id}/{code}/{kind}",
                       body={}, fetch=fetch, now=now)
    if verify and code in positions(sess, fetch=fetch, now=now):
        raise ArkApiError(f"寫入後讀回 {code} 仍在庫存，刪除視為未生效")
    return resp
