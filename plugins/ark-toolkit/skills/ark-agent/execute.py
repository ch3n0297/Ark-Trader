# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""下單層：把鎖定的決策翻成 Shioaji 委託並送出。

**兩個單位陷阱**，兩個都不會被上游擋到（journal 只看股數與代號，看不到最後
送進 API 的參數）：

1. `Common` 的 quantity 單位是**張**，`IntradayOdd` 是**股**。整個 toolkit 以
   股計價，所以 47 股的 2330 若當成 `Common, quantity=47` 送出，就是 47 張、
   一億一千萬的委託。`split_shares` 負責換算與拆腿。
2. 台股 **ETF 的價格檔位與個股不同**（ETF 兩段、個股六段）。檔位算錯會被券商
   拒單——安全失敗，不會亂成交，但當天就交易不了。

純函式（拆腿、檔位、限價、送出前檢查）不匯入 shioaji，任何平台可測；
連線只發生在 `place_legs` 與 `activate_ca`。
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import journal  # noqa: E402
import risk     # noqa: E402

COMMON = "Common"
INTRADAY_ODD = "IntradayOdd"
AFTER_HOURS_ODD = "Odd"          # 盤後零股：13:40–14:30 收單、14:30 單一價撮合，單位：股
ODD_MAX = 999
SHARES_PER_LOT = 1000

# (價格上界, 檔位)。ETF 兩段、個股六段——實證：帳戶內 00635U @ 45.03 落在
# 0.01 檔、0050 @ 104.25 落在 0.05 檔，套個股表（100–500 為 0.5）都不合法。
ETF_TICKS = ((50.0, 0.01), (math.inf, 0.05))
STOCK_TICKS = ((10.0, 0.01), (50.0, 0.05), (100.0, 0.1),
               (500.0, 0.5), (1000.0, 1.0), (math.inf, 5.0))

EPS = 1e-9

# 限價可以偏離參考價多少。掛得太寬等於下市價單：賣單限價低於市價 2%，就是
# 事先接受最差 −2% 的成交，而無人值守時沒人會發現這種滑價。
MAX_LIMIT_DEVIATION = 0.02


# ---------------------------------------------------------------- 股數與檔位

def split_shares(shares):
    """股數 → [(order_lot, quantity)]，quantity 已換成該 lot 的單位。

    整張走 Common（單位：張），畸零股走 IntradayOdd（單位：股，上限 999）。
    """
    if shares < 0:
        raise ValueError(f"股數不可為負：{shares}")
    lots, odd = divmod(shares, SHARES_PER_LOT)
    out = []
    if lots:
        out.append((COMMON, lots))
    if odd:
        out.append((INTRADAY_ODD, odd))
    return out


def split_shares_after_hours(shares):
    """股數 → [(Odd, quantity)]，盤後零股每筆上限 999 股，超過就拆成多筆。

    盤中補跑來不及時的補救通道（使用者 2026-08-21／09-10 認可）：整張也拆成
    零股送，因為盤後零股盤沒有整股委託。
    """
    if shares < 0:
        raise ValueError(f"股數不可為負：{shares}")
    out = []
    while shares > 0:
        qty = min(shares, ODD_MAX)
        out.append((AFTER_HOURS_ODD, qty))
        shares -= qty
    return out


def is_etf(code):
    """台股 ETF 代號一律以 00 開頭（0050、00635U、00631L…）。

    判錯的後果是檔位算錯 → 券商拒單 → 當天不交易，不會造成錯誤成交。
    """
    return code.startswith("00")


def tick_size(price, is_etf):
    table = ETF_TICKS if is_etf else STOCK_TICKS
    for upper, tick in table:
        if price < upper:
            return tick
    return table[-1][1]


def round_to_tick(price, is_etf, action):
    """把價格對齊到合法檔位。買進向下、賣出向上——都是對自己較保守的方向。"""
    tick = tick_size(price, is_etf)
    n = price / tick
    n = math.floor(n + EPS) if action == "buy" else math.ceil(n - EPS)
    return round(n * tick, 4)


# ---------------------------------------------------------------- 限價與委託腿

def limit_price(order, limits):
    """決策的限價區間 → 實際限價（夾在漲跌停內並對齊檔位）。

    買進取區間上緣、賣出取下緣——限價的意義是「最差可接受價」，取反方向
    等於大概率不成交。沒有限價區間就拒絕：無人值守系統不下市價單。
    """
    low, high = order.get("limit_low"), order.get("limit_high")
    if not low or not high:
        raise ValueError(f"{order['code']} 缺少限價區間，無人值守不下市價單")
    raw = high if order["action"] == "buy" else low
    clamped = min(limits["limit_up"], max(limits["limit_down"], raw))
    return round_to_tick(clamped, is_etf(order["code"]), order["action"])


def build_legs(order, limits, after_hours=False):
    """一筆決策 → 可直接送出的委託腿。`shares` 保留原始股數供成交對帳。
    `after_hours` 走盤後零股（全部以股計、每筆 ≤ 999）。"""
    price = limit_price(order, limits)
    pieces = (split_shares_after_hours(order["qty"]) if after_hours
              else split_shares(order["qty"]))
    return [{"code": order["code"], "action": order["action"],
             "order_lot": lot, "quantity": qty,
             "shares": qty * SHARES_PER_LOT if lot == COMMON else qty,
             "price": price}
            for lot, qty in pieces]


def execution_guard(orders, envelope, quotes, packet_hash):
    """送出前的最後一道檢查，回傳阻擋原因（空 = 放行）。

    縱深防禦：journal 驗 ARK 紀律、envelope 定執行邊界，送出前再查一次金額
    上限——前兩道任一出錯時，這道仍能把爆炸半徑鎖住。
    """
    out = []
    if envelope.get("packet_hash") != packet_hash:
        out.append("envelope 與決策的 packet_hash 不符——限額建立在別天的事實上")
    if orders and not envelope.get("can_buy") and any(o["action"] == "buy"
                                                      for o in orders):
        out.append("執行邊界：目前不可買進")
    if orders and not envelope.get("can_sell") and any(o["action"] == "sell"
                                                       for o in orders):
        out.append("執行邊界：目前不可賣出")
    out += limit_deviation_violations(orders, quotes)
    return out + risk.order_caps_violations(orders, quotes, envelope["limits"])


def limit_deviation_violations(orders, quotes):
    """限價偏離參考價過大者。

    賣單看 `limit_low`（願意接受的最低價）、買單看 `limit_high`（願付的最高價）——
    那才是實際會送出的價格，也是滑價的上限。沒有參考價時不判，交給金額上限把關。
    """
    out = []
    for o in orders:
        ref = (quotes.get(o["code"]) or {}).get("close")
        edge = o.get("limit_low") if o["action"] == "sell" else o.get("limit_high")
        if not ref or not edge:
            continue
        deviation = abs(edge - ref) / ref
        if deviation > MAX_LIMIT_DEVIATION:
            out.append(f"{o['code']} 限價 {edge} 偏離參考價 {ref} 達 {deviation:.1%}"
                       f"，超過 {MAX_LIMIT_DEVIATION:.0%}（等同下市價單）")
    return out


# ---------------------------------------------------------------- 連線與送出

def activate_ca(api, ca_path, ca_passwd, person_id):
    """啟用憑證。正式環境下單的前提，模擬環境不需要。"""
    if not os.path.exists(ca_path):
        raise RuntimeError(f"找不到憑證檔：{ca_path}")
    if not api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd,
                           person_id=person_id):
        raise RuntimeError("憑證啟用失敗——請檢查密碼與 person_id 是否與憑證相符")
    return True


def place_legs(api, legs):
    """送出委託腿，回傳每腿的結果（成功與失敗都回報，不中途拋出）。

    一腿失敗不該讓其餘的腿無聲消失——後續對帳要知道哪幾腿真的送出去了。
    """
    import shioaji as sj

    lots = {COMMON: sj.StockOrderLot.Common,
            INTRADAY_ODD: sj.StockOrderLot.IntradayOdd,
            AFTER_HOURS_ODD: sj.StockOrderLot.Odd}
    actions = {"buy": sj.Action.Buy, "sell": sj.Action.Sell}
    out = []
    for leg in legs:
        try:
            order = sj.StockOrder(
                action=actions[leg["action"]], price=leg["price"],
                quantity=leg["quantity"], price_type=sj.StockPriceType.LMT,
                order_type=sj.OrderType.ROD, order_lot=lots[leg["order_lot"]],
                order_cond=sj.StockOrderCond.Cash, account=api.stock_account)
            trade = api.place_order(api.Contracts.Stocks[leg["code"]], order)
            out.append({**leg, "ok": True, "order_id": trade.status.id,
                        "seqno": trade.order.seqno,
                        "status": str(trade.status.status)})
        except Exception as e:                                    # noqa: BLE001
            out.append({**leg, "ok": False,
                        "error": f"{type(e).__name__}: {e}"})
    return out


def contract_limits(api, code):
    c = api.Contracts.Stocks[code]
    return {"limit_up": float(c.limit_up), "limit_down": float(c.limit_down),
            "reference": float(c.reference)}


# ---------------------------------------------------------------- 等成交

# 委託不會再變的狀態。PartFilled 不算——剩下的量還掛著。
TERMINAL_STATUSES = frozenset({"Filled", "Cancelled", "Failed", "Inactive"})


def status_name(status):
    """`OrderStatus.Filled` 或 'OrderStatus.Filled' → 'Filled'。"""
    return str(status).rsplit(".", 1)[-1]


def pending_seqnos(results, statuses):
    """送出成功的腿裡，還沒到終態的 seqno。`statuses` = {seqno: 狀態}。

    查不到狀態的腿保守視為未完成——寧可多等一輪，不可漏刪一筆還掛著的單。
    """
    return [r["seqno"] for r in results if r.get("ok")
            and status_name(statuses.get(r["seqno"], "")) not in TERMINAL_STATUSES]


def wait_for_fills(api, results, minutes, poll_seconds=20,
                   clock=time.monotonic, sleep=time.sleep):
    """輪詢委託直到全部終態或逾時；逾時仍掛著的腿刪單。回傳 (statuses, 刪單的 seqno)。

    成交後要立刻結算當日（daily.py trade），「交易完成」必須有明確定義：
    每一腿都成交或已刪。逾時不追價——R-008 的精神，也讓結算後不會再有帳外變化。
    """
    deadline = clock() + minutes * 60
    while True:
        api.update_status(api.stock_account)
        trades = {t.order.seqno: t for t in api.list_trades()}
        statuses = {s: status_name(t.status.status) for s, t in trades.items()}
        pending = pending_seqnos(results, statuses)
        if not pending:
            return statuses, []
        if clock() >= deadline:
            break
        sleep(poll_seconds)

    cancelled = []
    for seqno in pending:
        trade = trades.get(seqno)
        if trade is None:
            continue
        api.cancel_order(trade)
        cancelled.append(seqno)
    api.update_status(api.stock_account)
    statuses = {t.order.seqno: status_name(t.status.status) for t in api.list_trades()}
    return statuses, cancelled


# ---------------------------------------------------------------- CLI

def main():
    import market
    import packet as packet_mod

    ap = argparse.ArgumentParser(description="送出已鎖定的決策")
    ap.add_argument("--backend", choices=("simulation", "live"), required=True,
                    help="simulation 走模擬環境；live 需憑證，會動真錢")
    ap.add_argument("--date", help="覆寫決策日（預設今天）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只印出將送出的委託腿，不連線")
    ap.add_argument("--latest", action="store_true",
                    help="送同日最後一筆（amended）決策；預設送第一筆")
    ap.add_argument("--after-hours", action="store_true",
                    help="走盤後零股（13:40–14:30 收單、14:30 單一價）；盤中補跑來不及時用")
    ap.add_argument("--wait", type=float, metavar="MINUTES",
                    help="送單後留在線上等成交最多幾分鐘，逾時仍掛著的腿刪單（成交後立刻結算用）")
    args = ap.parse_args()

    date = args.date or dt.date.today().isoformat()
    pk = packet_mod.load_packet(date)
    pick = journal.latest_decision if args.latest else journal.first_decision
    decision = pick(journal.load_entries(), date)
    env_path = os.path.join(risk.ENVELOPE_DIR, f"{date}.json")
    if pk is None or decision is None or not os.path.exists(env_path):
        missing = [n for n, ok in (("決策包", pk), ("決策", decision),
                                   ("執行邊界", os.path.exists(env_path))) if not ok]
        print(f"🛑 缺少 {'、'.join(missing)}，不執行", file=sys.stderr)
        return 2
    with open(env_path, encoding="utf-8") as fh:
        envelope = json.load(fh)

    orders = decision.get("orders", [])
    if not orders:
        print("本日決策為不動作，無委託可送")
        return 0

    blocked = execution_guard(orders, envelope,
                              pk.get("market", {}).get("quotes", {}), pk["hash"])
    if blocked:
        print("🛑 送出前檢查未通過：", file=sys.stderr)
        for b in blocked:
            print(f"  - {b}", file=sys.stderr)
        return 2

    if args.dry_run:
        with market.session() as api:
            for o in orders:
                for leg in build_legs(o, contract_limits(api, o["code"]), args.after_hours):
                    print(f"  {leg['action']} {leg['code']} "
                          f"{leg['order_lot']} qty={leg['quantity']}"
                          f"（{leg['shares']} 股）@ {leg['price']}")
        return 0

    import shioaji as sj
    import source
    source.load_credentials()
    api = sj.Shioaji(simulation=args.backend == "simulation")
    api.login(api_key=os.environ["SHIOAJI_API_KEY"],
              secret_key=os.environ["SHIOAJI_SECRET_KEY"])
    statuses, cancelled = {}, []
    try:
        if args.backend == "live":
            activate_ca(api, os.environ["SHIOAJI_CA_PATH"],
                        os.environ["SHIOAJI_CA_PASSWORD"],
                        api.stock_account.person_id)
        results = []
        for o in orders:
            results += place_legs(api, build_legs(o, contract_limits(api, o["code"]),
                                                  args.after_hours))
        api.update_status(api.stock_account)
        # 先落地再等：等待途中行程若死掉，日誌仍看得到哪幾腿已經送出去了
        journal.append_entry({
            "type": "execution", "date": date, "backend": args.backend,
            "ts": dt.datetime.now().replace(microsecond=0).isoformat(),
            "decision_lock": decision["lock"], "legs": results,
            **({"channel": "after_hours_odd"} if args.after_hours else {})})
        if args.wait:
            statuses, cancelled = wait_for_fills(api, results, args.wait)
    finally:
        api.logout()

    if cancelled:
        journal.append_entry({
            "type": "cancel", "date": date,
            "ts": dt.datetime.now().replace(microsecond=0).isoformat(),
            "decision_lock": decision["lock"], "seqnos": cancelled,
            "reason": f"送單後 {args.wait:g} 分未成交，刪單以便當日結算"})

    ok = sum(1 for r in results if r["ok"])
    print(f"{'✅' if ok == len(results) else '⚠️'} 送出 {ok}/{len(results)} 腿"
          f"（{args.backend}）")
    for r in results:
        mark = "✓" if r["ok"] else "✗"
        detail = (f"id={r['order_id']} {r['status']}" if r["ok"] else r["error"])
        if r["ok"] and args.wait:
            final = statuses.get(r["seqno"], "?")
            detail += f" → {final}" + ("（已刪單）" if r["seqno"] in cancelled else "")
        print(f"  {mark} {r['action']} {r['code']} {r['shares']} 股 @ {r['price']}"
              f"　{detail}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
