# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "pyobjc-framework-cocoa",
#     "pyobjc-framework-applicationservices",
#     "pyobjc-framework-quartz",
#     "python-dotenv",
#     "shioaji",
# ]
# ///
"""決策日誌：Agent 建議的鎖定、硬規則驗證、成交對回。

鎖定機制 = append-only JSONL + 內容雜湊 + first-decision-wins：
決策寫入後不可改，同日第二筆標 `amended`（誠實呈現，不靜默取代），
evaluate 只認每個日期的第一筆。買賣建議只存在於這裡——
ark-analyze 的「不產出買賣建議」不變量不受影響。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import ark      # noqa: E402
import market   # noqa: E402
import tracks   # noqa: E402

JOURNAL = os.path.expanduser("~/.ark-toolkit/agent/journal.jsonl")

CORE, SATELLITE = tracks.CORE, tracks.SATELLITE
INHERITED, FROZEN = tracks.INHERITED, tracks.FROZEN


# ---------------------------------------------------------------- 鎖定與讀寫

def lock_hash(entry):
    """canonical JSON（排除 lock 欄自身）的 sha256。"""
    body = {k: v for k, v in entry.items() if k != "lock"}
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def append_entry(entry, path=JOURNAL):
    ark.append_sync_log(entry, path)


def load_entries(path=JOURNAL):
    """讀全部日誌，壞行略過（比照 ark.last_sync：紀錄壞一行不該讓主流程掛掉）。"""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def first_decision(entries, date):
    """該日期的第一筆決策——evaluate 只認它，之後的都是 amended。"""
    for e in entries:
        if e.get("type") == "decision" and e.get("date") == date:
            return e
    return None


def latest_decision(entries, date):
    """該日期的最後一筆決策——送出 amended 決策時用；evaluate 仍只認第一筆。"""
    picked = None
    for e in entries:
        if e.get("type") == "decision" and e.get("date") == date:
            picked = e
    return picked


# ---------------------------------------------------------------- 驗證（硬規則）

def _est_price(order, quotes):
    """估價用前一日收盤，沒有報價就用限價區間中點。"""
    q = quotes.get(order["code"])
    if q and q.get("close"):
        return float(q["close"])
    return (order.get("limit_low", 0.0) + order.get("limit_high", 0.0)) / 2


def track_of(order):
    """委託所屬軌道；未標示者視為主軌（既有決策格式不必改就仍然合法）。"""
    return order.get("track", CORE)


def is_leveraged_or_inverse(code):
    """台股槓桿（正2，代號結尾 L）與反向（反1，結尾 R）ETF。

    主軌不停損，標的必須會均值回歸；這類產品有波動耗損，盤整市慢性失血
    且不保證回得來，沒有出場機制的軌道抱它是最糟的組合。它們在 ARK 的
    價值區裡是合法候選，所以得在紀律層擋。
    """
    return code[:2] == "00" and code[-1:] in ("L", "R")


def _validate_satellite(sat, envelope, quotes, positions):
    """衛星軌自己的規則：白名單、配額、冷卻、單檔／產業上限。

    ARK 紀律（獲利才賣、只買價值區、檔數）不套用在這裡——這一軌存在的理由
    就是做 ARK 不做的事（停損、非價值區標的）。但它不是法外之地：
    白名單＝當日 ARK 產業價值區候選 ∪ 目前持有 ∪ tracks.json 指定，
    金額受配額與單檔／產業上限限制。
    """
    if envelope is None:
        return ["有衛星軌委託但缺少執行邊界——查不到配額與白名單，不放行"]
    st = envelope["tracks"][SATELLITE]
    out = []
    allow = set(st.get("allowlist", ()))
    for o in sat:
        if o["code"] not in allow:
            out.append(f"{o['code']} 不在衛星軌白名單內"
                       f"（白名單＝當日 ARK 產業價值區候選 ∪ 目前持有 ∪ tracks.json 指定）")
    buys = [o for o in sat if o["action"] == "buy"]
    # 對已歸他軌的持倉加碼＝把它偷偷搬進停損軌。換軌是人的決定，賣出不受影響。
    for track in (INHERITED, CORE):
        held = set(envelope["tracks"].get(track, {}).get("codes", ()))
        for o in buys:
            if o["code"] in held:
                out.append(f"{o['code']} 目前歸 {track} 軌，個股桶不可對它加碼"
                           f"（要換軌請改 tracks.json）")
    if buys and st.get("halted"):
        out.append(f"衛星軌冷卻中不可新倉：{st.get('halt_reason')}")
    caps = envelope.get("satellite", {})
    name_cap = caps.get("name_cap_value")
    sector_cap = caps.get("sector_cap_value")
    sectors = caps.get("sectors", {})
    exposure = dict(caps.get("sector_exposure", {}))
    after_by_code = {}
    for o in buys:
        value = o["qty"] * _est_price(o, quotes)
        if o["code"] not in after_by_code:
            held = positions.get(o["code"], {})
            after_by_code[o["code"]] = (
                held.get("qty", 0) * held.get("last_price", _est_price(o, quotes)))
        after_by_code[o["code"]] += value
        after = after_by_code[o["code"]]
        if name_cap is not None and after > name_cap:
            out.append(f"{o['code']} 買進後市值 {after:,.0f} 超過單檔上限 {name_cap:,.0f}")
        key = sectors.get(o["code"]) or o["code"]
        exposure[key] = exposure.get(key, 0.0) + value
        if sector_cap is not None and exposure[key] > sector_cap:
            out.append(f"{o['code']} 買進後「{key}」合計 {exposure[key]:,.0f} 超過產業上限 {sector_cap:,.0f}")
    net = (sum(o["qty"] * _est_price(o, quotes) for o in buys)
           - sum(o["qty"] * _est_price(o, quotes)
                 for o in sat if o["action"] == "sell"))
    # 配額約束的是**買進**。淨賣出一律放行——remaining 為負代表已經超額，
    # 而賣出正是唯一能修正它的動作；用 net > remaining 一律比較會把減碼也擋掉。
    if net > 0 and net > st["remaining"]:
        out.append(f"衛星軌淨買進 {net:,.0f} 超過剩餘配額 {st['remaining']:,.0f}")
    return out


def _required_lamp_exits(sat, envelope):
    """`envelope.satellite.lamp_exits`（確認無燈號、且在使用者設定的範圍內）必須整筆賣出。

    範圍預設關閉，清單只在使用者開啟時出現；燈號未知的部位不會進清單。
    不能賣的日子（L2 熔斷、事實不可信）不強制——那天本來就一筆都不能動。
    """
    if not envelope or not envelope.get("can_sell"):
        return []
    sold = {}
    for o in sat:
        if o["action"] == "sell":
            sold[o["code"]] = sold.get(o["code"], 0) + o["qty"]
    return [f"個股桶 {x['code']} 已確認無燈號（價值否、升溫否），依使用者出場判準必須整筆賣出："
            f"持有 {x['qty']} 股，委託 {sold.get(x['code'], 0)} 股"
            for x in envelope.get("satellite", {}).get("lamp_exits", ())
            if sold.get(x["code"], 0) < x["qty"]]


def _validate_frozen(frz, envelope, packet):
    """凍結軌的出場規則：碎片部位（低於最小可交易金額）只准**整筆清掉**，
    且該標的已不在 ARK 價值區。

    這是對主軌「獲利才賣」的明示偏離（使用者 2026-09-07 決定）：碎片對集中與
    分散都沒貢獻，留著只是帳面雜訊；但仍在價值區的碎片是 ARK 認為值得持有的，
    照舊留著等回本。凍結軌不可買進——低於最小金額的部位要嘛清掉、要嘛加碼到
    主軌，加碼走主軌規則。
    """
    if envelope is None:
        return ["有凍結軌委託但缺少執行邊界——查不到凍結清單，不放行"]
    frozen_codes = set(envelope["tracks"].get(FROZEN, {}).get("codes", ()))
    positions = packet.get("account", {}).get("positions", {})
    holdings = packet.get("ark", {}).get("holdings", {})
    out = []
    for o in frz:
        code = o["code"]
        if o["action"] != "sell":
            out.append(f"凍結軌只能清倉，不可買進 {code}")
            continue
        if code not in frozen_codes:
            out.append(f"{code} 不在凍結軌（envelope.tracks.frozen）")
            continue
        held = positions.get(code, {}).get("qty", 0)
        if o["qty"] != held:
            out.append(f"凍結軌 {code} 只准整筆清掉：持有 {held} 股，委託 {o['qty']} 股")
        if "價值" in holdings.get(code, {}).get("tiers", []):
            out.append(f"凍結軌 {code} 仍在 ARK 價值區，照主軌規則留著等回本")
    return out


def validate_orders(orders, packet, envelope=None):
    """硬規則驗證，回傳違規清單（空 = 合規）。

    主軌（core）＝「完全比照 ARK 紀律」，違反硬規則是 bug 不是自由度；
    自由度（賣哪幾檔、股數偏離、限價）不在這裡擋，事後由 evaluate 量測。
    衛星軌（satellite）走 `_validate_satellite`，不受 ARK 紀律約束；
    凍結軌（frozen）走 `_validate_frozen`，只准整筆清掉已離開價值區的碎片。

    **「先調節才可買」兩軌都適用**：那是 ARK 的帳戶級曝險訊號，若衛星軌能在
    被要求調節時加碼，帳戶總曝險就失控了。
    """
    disc = packet["discipline"]
    quotes = packet.get("market", {}).get("quotes", {})
    positions = packet.get("account", {}).get("positions", {})
    scope = set(packet.get("news_scope", {}).get("codes", []))
    core = [o for o in orders if track_of(o) == CORE]
    sat = [o for o in orders if track_of(o) == SATELLITE]
    frz = [o for o in orders if track_of(o) == FROZEN]
    violations = [f"{o['code']} 的軌道不明：{track_of(o)!r}"
                  for o in orders if track_of(o) not in (CORE, SATELLITE, FROZEN)]

    # 資訊邊界是 ARK 實驗協定的一部分，只約束主軌；衛星軌以白名單為界
    for o in core:
        if o["code"] not in scope:
            violations.append(f"{o['code']} 不在 news_scope 清單內（資訊邊界外的標的）")
    for o in core:
        if o["action"] == "sell" and o["code"] not in disc["sellable"]:
            violations.append(f"賣出 {o['code']} 違反紀律：不在可調節清單（虧損中或非持股）")
        if o["action"] == "buy" and o["code"] not in disc["buy_candidates"]:
            violations.append(f"買進 {o['code']} 違反紀律：不在價值區候選清單")
        if o["action"] == "buy" and is_leveraged_or_inverse(o["code"]):
            violations.append(f"買進 {o['code']} 違反主軌設定：槓桿／反向 ETF "
                              f"有波動耗損，不可放進沒有停損的主軌（衛星軌才行）")
    if sat:
        violations += _validate_satellite(sat, envelope, quotes, positions)
    violations += _required_lamp_exits(sat, envelope)
    if frz:
        violations += _validate_frozen(frz, envelope, packet)

    buys = [o for o in orders if o["action"] == "buy"]
    sells = [o for o in orders if o["action"] == "sell"]
    if buys and disc["adjust_required_before_buy"]:
        sell_value = sum(o["qty"] * _est_price(o, quotes) for o in sells)
        if sell_value < disc["adjust_amount"]:
            violations.append(
                f"參考調節 {disc['adjust_amount']:,.0f} 未被賣單覆蓋"
                f"（賣單估值 {sell_value:,.0f}）——App 紀律：先調節才可買")

    # 檔數是 ARK 模型的概念，只算主軌部位；其餘三軌不在 ARK 的分配模型裡
    core_positions = set(positions) - non_core_codes(envelope)
    core_sells = [o for o in core if o["action"] == "sell"]
    fully_sold = {o["code"] for o in core_sells
                  if o["code"] in positions and o["qty"] >= positions[o["code"]]["qty"]}
    names_after = ((core_positions - fully_sold)
                   | {o["code"] for o in core if o["action"] == "buy"})
    # 既有超限不歸咎於本日決策——只擋「讓檔數變得更糟」的單
    allowed = max(disc["max_names"], len(core_positions))
    if len(names_after) > allowed and disc.get("names_cap", "hard") != "advisory":
        violations.append(
            f"執行後主軌將持有 {len(names_after)} 檔，超過檔數上限 {disc['max_names']}")
    return violations


def non_core_codes(envelope):
    """不屬於主軌的代號（衛星／繼承／凍結）。無 envelope 時視為全部都在主軌。"""
    if envelope is None:
        return set()
    return {c for t in (SATELLITE, INHERITED, FROZEN)
            for c in envelope["tracks"].get(t, {}).get("codes", ())}


def load_envelope(date):
    """當日的執行邊界；沒有則回 None（此時衛星軌委託一律不放行）。

    在這裡讀而不由呼叫端傳入：record 是 CLI 入口，忘了帶 envelope 會讓
    衛星軌規則靜默失效，而那正是最不該靠人記得的地方。
    """
    import risk
    path = os.path.join(risk.ENVELOPE_DIR, f"{date}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def record_decision(decision, packet, entries, override=False, envelope=None):
    """驗證並準備寫入的決策 entry，回傳 (entry 或 None, violations)。

    packet_hash 必須綁定當日決策包——證明決策看的是哪一份資料，
    對舊資料做的決策不可掛到新包上。
    """
    if packet is None:
        return None, ["當日決策包不存在，先執行 packet.py"]
    if decision.get("packet_hash") != packet["hash"]:
        return None, ["packet_hash 與當日決策包不符（決策依據的不是這份資料）"]
    violations = validate_orders(decision.get("orders", []), packet, envelope)
    if violations and not override:
        return None, violations
    entry = dict(decision)
    entry["type"] = "decision"
    if violations:
        entry["violations"] = violations
    if first_decision(entries, decision["date"]) is not None:
        entry["amended"] = True
    entry["lock"] = lock_hash(entry)
    return entry, violations


# ---------------------------------------------------------------- 成交對回

def infer_buy_price(old_qty, old_avg, new_qty, new_avg):
    """買進成交價由持倉均價反推——list_profit_loss 只有賣出，買進得用 diff。"""
    return (new_qty * new_avg - old_qty * old_avg) / (new_qty - old_qty)


def raw_positions(positions):
    """對回用的事前持倉：均價一律取券商原值（`raw_avg_price`），與事後的
    list_positions 同口徑。舊決策包沒有原值欄時退用 `avg_price`（若那是含費
    口徑，反推會偏低——2026-08-22～09-03 的對回即因此重算過）。"""
    return {code: {"qty": p["qty"],
                   "avg_price": p.get("raw_avg_price", p["avg_price"])}
            for code, p in positions.items()}


def _slippage(price, order):
    low, high = order.get("limit_low"), order.get("limit_high")
    if not low or not high:
        return None
    mid = (low + high) / 2
    return (price - mid) / mid


def external_legs(entries, date, own_lock):
    """同日**計畫外**的成交腿：phase0，或非第一決策（amended）的送單。

    對回把「該代號當日全部賣出」都算給決策單——同日若有計畫外執行，不先
    扣掉就會把它的量與價都歸給決策（2026-08-11 實例：決策賣 2330 8 股、
    phase0 又賣 24 股，會被記成決策成交 32 股）。本決策自己的 execution
    以 decision_lock 識別，不論 backend 都不算計畫外。
    """
    return [leg for e in entries
            if e.get("date") == date
            and (e.get("type") == "phase0"
                 or (e.get("type") == "execution"
                     and e.get("decision_lock") != own_lock))
            for leg in e.get("legs", []) if leg.get("ok")]


def deduct_external_legs(pl_rows, legs):
    """從損益列扣掉計畫外賣出，回傳剩餘列（歸屬決策單的部分）。

    腿記的是委託價非成交價，不能按價格對列——按股數扣：先找股數恰好
    相等的列整列對消（分筆送單的常態），沒有再依序扣量。
    """
    remaining = [dict(r) for r in pl_rows]
    for leg in legs:
        if leg.get("action") != "sell":
            continue
        need = leg["shares"]
        exact = next((r for r in remaining
                      if r["code"] == leg["code"] and r["quantity"] == need), None)
        if exact is not None:
            remaining.remove(exact)
            continue
        for r in remaining:
            if need <= 0:
                break
            if r["code"] != leg["code"]:
                continue
            take = min(need, r["quantity"])
            r["quantity"] -= take
            need -= take
        remaining = [r for r in remaining if r["quantity"] > 0]
    return remaining


def match_fills(orders, pl_rows, positions_before, positions_after,
                external_buys=None):
    """把決策單對回實際成交，回傳 (fills, unfilled)。

    賣出：list_profit_loss 的列直接給成交價（可能多筆，加權平均）——
    呼叫端應先以 deduct_external_legs 扣掉計畫外賣出。
    買進：持倉 diff + 均價反推；external_buys（{code: 股數}）是同日
    計畫外買進，從 diff 扣掉（均價反推會含它的價格，屬近似值）。
    """
    fills, unfilled = [], []
    external_buys = external_buys or {}
    for o in orders:
        code = o["code"]
        if o["action"] == "sell":
            rows = [r for r in pl_rows if r["code"] == code]
            qty = sum(r["quantity"] for r in rows)
            if qty:
                price = sum(r["quantity"] * r["price"] for r in rows) / qty
                fills.append({"code": code, "action": "sell", "qty": qty,
                              "price": round(price, 4), "source": "profit_loss",
                              "slippage": _slippage(price, o)})
            else:
                unfilled.append({"code": code, "action": "sell",
                                 "reason": "當日無賣出成交"})
        else:
            before = positions_before.get(code, {"qty": 0, "avg_price": 0.0})
            after = positions_after.get(code)
            gained = ((after["qty"] if after else 0) - before["qty"]
                      - external_buys.get(code, 0))
            if gained > 0:
                price = infer_buy_price(before["qty"], before["avg_price"],
                                        after["qty"], after["avg_price"])
                fills.append({"code": code, "action": "buy", "qty": gained,
                              "price": round(price, 4), "source": "position_diff",
                              "slippage": _slippage(price, o)})
            else:
                unfilled.append({"code": code, "action": "buy",
                                 "reason": "持倉未增加（當日無買進成交）"})
    return fills, unfilled


def satellite_exits_from(decision, fills, before, stop_loss):
    """個股桶賣單的出場紀錄：以決策包的原值均價算 roi，跌破停損標 stopped。
    `risk.satellite_cooldown` 靠這份紀錄數連續停損——沒有寫入端它永遠不會生效。"""
    filled = {(f["action"], f["code"]): f for f in fills}
    out = []
    for o in decision.get("orders", []):
        if o.get("track") != SATELLITE or o["action"] != "sell":
            continue
        f = filled.get(("sell", o["code"]))
        avg = before.get(o["code"], {}).get("avg_price")
        if not f or not avg:
            continue
        roi = f["price"] / avg - 1
        out.append({"date": decision.get("date"), "code": o["code"], "qty": f["qty"],
                    "price": f["price"], "roi": round(roi, 6), "stopped": roi <= stop_loss})
    return out


def append_exits(records, path=None):
    import risk
    path = path or risk.EXITS
    if not records:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def satellite_assignments(decision, fills, assigned):
    """成交的衛星軌買單，若代號尚未指派衛星軌則新增指派，回傳新的指派表。

    個股桶會買 ARK 候選裡沒人寫進 tracks.json 的代號；`tracks.resolve` 對未指派
    代號預設 core（無停損、套用 ARK 紀律）——沒有這道，填單成交後隔天就會被
    當成主軌部位。沒有東西要加時回傳原物件（不複製），呼叫端可用來判斷是否
    需要落地。"""
    filled_buys = {f["code"] for f in fills if f.get("action") == "buy"}
    out = assigned
    for o in decision.get("orders", []):
        if o.get("track") != SATELLITE or o.get("action") != "buy":
            continue
        code = o["code"]
        # 只補「還沒人指定」的新倉。已歸別軌者由人改 tracks.json——自動搬軌會讓
        # 「這筆為什麼被停損」變得無法解釋。
        if code not in filled_buys or code in out:
            continue
        out = tracks.assign(out, code, SATELLITE)
    return out


def persist_satellite_assignments(decision, fills, path=None):
    """`satellite_assignments` 的落地：讀現有 tracks.json，有新增才寫回。"""
    path = path or tracks.TRACKS_PATH
    assigned = tracks.load(path)
    updated = satellite_assignments(decision, fills, assigned)
    if updated is not assigned:
        tracks.save(updated, path)


def previous_weekday(date_iso):
    d = dt.date.fromisoformat(date_iso) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def settle_previous(api, today, path=JOURNAL, price_dir=None):
    """結算前一交易日的決策成交（packet 盤前順手呼叫）。回傳寫入的 entry 或 None。"""
    return settle_day(api, previous_weekday(today), today, path, price_dir)


def settle_day(api, day, today, path=JOURNAL, price_dir=None):
    """結算 `day` 的決策成交，以 `today` 為記錄日。回傳寫入的 entry 或 None。

    `day` 若休市（國定假日，以 0050 當日有無行情判定）不算缺席；
    已結算或已記缺席的日期不重複寫。`day == today` 是成交後立刻結算
    （daily.py trade）——成交比對用的是決策包的盤前持倉對現在持倉，當天就能算。
    """
    import shioaji as sj
    import packet as packet_mod

    kwargs = {"directory": price_dir} if price_dir else {}
    if not market.fetch_daily(api, packet_mod.BENCHMARK, day, day, **kwargs):
        return None                      # 休市日
    entries = load_entries(path)
    if any(e.get("type") == "fill" and e.get("decision_ref") == day for e in entries):
        return None
    if any(e.get("type") == "missed" and e.get("date") == day for e in entries):
        return None

    now = dt.datetime.now().replace(microsecond=0).isoformat()
    decision = first_decision(entries, day)
    if decision is None:
        pk = packet_mod.load_packet(day)
        entry = {"type": "missed", "date": day, "ts": now,
                 "reason": "未做決策" if pk else "packet 未產生"}
        append_entry(entry, path)
        return entry
    if decision.get("no_trade"):
        entry = {"type": "fill", "date": today, "ts": now, "decision_ref": day,
                 "fills": [], "unfilled": [], "note": "no_trade"}
        append_entry(entry, path)
        return entry

    pl = api.list_profit_loss(api.stock_account, day, day, unit=sj.Unit.Share)
    pl_rows = [{"code": r.code, "quantity": int(r.quantity), "price": float(r.price)}
               for r in pl]
    after = {p.code: {"qty": int(p.quantity), "avg_price": float(p.price)}
             for p in api.list_positions(api.stock_account, unit=sj.Unit.Share)}
    pk = packet_mod.load_packet(day)
    before = raw_positions((pk or {}).get("account", {}).get("positions", {}))
    ext = external_legs(entries, day, decision.get("lock"))
    ext_buys = {}
    for leg in ext:
        if leg.get("action") == "buy":
            ext_buys[leg["code"]] = ext_buys.get(leg["code"], 0) + leg["shares"]
    fills, unfilled = match_fills(decision.get("orders", []),
                                  deduct_external_legs(pl_rows, ext),
                                  before, after, external_buys=ext_buys)
    entry = {"type": "fill", "date": today, "ts": now, "decision_ref": day,
             "fills": fills, "unfilled": unfilled}
    append_entry(entry, path)

    try:
        import risk
        env = load_envelope(day)
        stop_loss = ((env or {}).get("satellite", {}).get("stop_loss")
                     or risk.DEFAULTS["satellite_stop_loss"])
        append_exits(satellite_exits_from({**decision, "date": day}, fills, before, stop_loss))
        persist_satellite_assignments(decision, fills)
    except Exception as exc:
        print(f"⚠️ 停損出場紀錄／衛星軌指派寫入失敗（成交紀錄已保存）：{exc}", file=sys.stderr)

    return entry


# ---------------------------------------------------------------- CLI

def main():
    import packet as packet_mod

    ap = argparse.ArgumentParser(description="ark-agent 決策日誌")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="寫入一筆決策（鎖定，不可改）")
    rec.add_argument("decision_file", help="Agent 組好的決策 JSON 檔")
    rec.add_argument("--override", action="store_true",
                     help="硬規則違規仍寫入（entry 會帶 violations，紀律報告照列）")
    st = sub.add_parser("settle", help="結算前一交易日的成交")
    st.add_argument("--date", help="以此日為「今天」（預設今天）")
    st.add_argument("--same-day", action="store_true",
                    help="改結算「今天」的決策（成交後立刻結算，daily.py trade 用）")
    sh = sub.add_parser("show", help="列出日誌")
    sh.add_argument("--date", help="只列此日期相關的紀錄")
    args = ap.parse_args()

    if args.cmd == "record":
        with open(args.decision_file, encoding="utf-8") as fh:
            decision = json.load(fh)
        decision.setdefault("ts", dt.datetime.now().replace(microsecond=0).isoformat())
        pk = packet_mod.load_packet(decision["date"])
        entries = load_entries()
        entry, violations = record_decision(decision, pk, entries, args.override,
                                            load_envelope(decision["date"]))
        if entry is None:
            print("🛑 拒絕寫入：")
            for v in violations:
                print(f"  - {v}")
            return 2
        append_entry(entry)
        n = len(entry.get("orders", []))
        print(f"✅ 決策已鎖定：{entry['date']}　{n} 筆單　lock={entry['lock'][:19]}…")
        if entry.get("amended"):
            print("  ⚠️ 本日已有決策，這筆標記為 amended——evaluate 只認第一筆")
        for v in violations:
            print(f"  ⚠️ 違規（--override 已寫入）：{v}")
        return 0

    if args.cmd == "settle":
        today = args.date or dt.date.today().isoformat()
        with market.session() as api:
            entry = (settle_day(api, today, today) if args.same_day
                     else settle_previous(api, today))
        if entry is None:
            print("無需結算（已結算、已記缺席、或休市）")
        elif entry["type"] == "missed":
            print(f"⚠️ {entry['date']} 缺席：{entry['reason']}")
        else:
            print(f"✅ 已結算 {entry['decision_ref']}："
                  f"成交 {len(entry['fills'])} 筆、未成交 {len(entry['unfilled'])} 筆")
            for f in entry["fills"]:
                slip = f" 滑價 {f['slippage']:+.2%}" if f.get("slippage") is not None else ""
                print(f"  {f['action']} {f['code']} {f['qty']} 股 @ {f['price']}{slip}")
        return 0

    entries = load_entries()
    if args.date:
        entries = [e for e in entries
                   if e.get("date") == args.date or e.get("decision_ref") == args.date]
    for e in entries:
        print(json.dumps(e, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
