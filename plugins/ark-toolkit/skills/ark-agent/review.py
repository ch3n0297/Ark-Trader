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
"""決策複盤：把一段期間的決策、成交、前瞻報酬與紀律分組成逐筆案例。

與 evaluate.py 的分工——evaluate 回答「成績如何」（聚合統計，服務實驗協定的
量化評估）；review 回答「當初為什麼這樣想、下次該改什麼」（逐筆案例與準則
損益歸因，服務改善迴路）。前瞻報酬與紀律分直接復用 evaluate 的函式，不重算。

**複盤看全部委託，不只主軌**：實驗統計刻意排除衛星軌以免污染數據，但衛星軌
那些單同樣花掉資金、同樣是判斷的產物，濾掉的話「為什麼買它」永遠不會被檢討。
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "lib")))

import evaluate  # noqa: E402
import journal   # noqa: E402
import market    # noqa: E402
import packet as packet_mod  # noqa: E402

ENVELOPE_DIR = os.path.expanduser("~/.ark-toolkit/agent/envelopes")


def load_envelope(date, directory=ENVELOPE_DIR):
    """讀某日的執行邊界；沒有就回 None（那天 risk.py 沒跑成或還沒有這個機制）。"""
    path = os.path.join(directory, f"{date}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 純邏輯

def in_range(date, since, until):
    """日期是否落在複盤區間內（含端點）。兩端可省略表示不設限。"""
    return (since is None or date >= since) and (until is None or date <= until)


def default_window(today, days=7):
    """預設複盤範圍：含今天在內的最近 days 天。

    使用者說「這週」指的是最近一週，不是全部歷史——複盤要能聚焦在
    還記得脈絡的那幾天，翻半年前的單於事無補。
    """
    end = dt.date.fromisoformat(today)
    return (end - dt.timedelta(days=days - 1)).isoformat(), end.isoformat()


def all_orders(decision):
    """全部委託（含衛星軌）。傳給 evaluate_all 覆寫它預設的主軌過濾。"""
    return decision.get("orders", [])


def build_context(packet, envelope):
    """決策當時的事實脈絡：方向依據、檔數上限、賣出優先序、跨檔候選比較。

    決策理由裡的數字要能在同一份輸出裡驗證。實例（2026-08-14）：理由寫
    「0050 位階金額 107 排第五」「sell_priority 首位」，那些數字原本散在
    packets/ 與 envelopes/，回溯討論得同時翻四個檔案才拼得回當時的情境——
    記錄完整卻無法自足。
    """
    if not packet:
        return None
    posture = packet.get("ark", {}).get("posture", {})
    disc = packet.get("discipline", {})
    core = (envelope or {}).get("core", {})
    rows = packet.get("ark", {}).get("layout", {}).get("rows", {})
    candidates = sorted(
        ({"code": code, "tiers": r.get("tiers", []),
          "tier_amount": r.get("tier_amount"), "premium": r.get("premium"),
          "price": r.get("price")} for code, r in rows.items()),
        key=lambda c: c["tier_amount"] or 0, reverse=True)
    return {
        "posture": {
            "suggested": posture.get("suggested_ratio"),
            "actual": posture.get("actual_ratio"),
            "gap": posture.get("gap"),
            "action": core.get("ratio_band", {}).get("action"),
        },
        # App 值與生效值不同時才解釋得了「為什麼滿檔」：App 說可以 N 檔，
        # 但遲滯把生效值壓在更低，於是買新標的就必須先賣掉手上那檔。
        "max_names": {"app": disc.get("max_names"), "effective": core.get("max_names")},
        "sell_priority": disc.get("sell_priority", []),
        "sellable": disc.get("sellable", []),
        "breaker": (envelope or {}).get("breaker", {}).get("level"),
        "candidates": candidates,
        "satellite": {
            "gates": (envelope or {}).get("satellite", {}).get("gates"),
            "trim_candidates": [c["code"] for c in
                                (envelope or {}).get("satellite", {}).get("trim_candidates", [])],
            "candidates_n": len(packet.get("industry", {}).get("candidates", {})),
            "candidates_source": (envelope or {}).get("satellite", {}).get("candidates_source"),
        },
    }


def cases_from(entries, report, since, until, contexts=None):
    """組出複盤案例：一個決策日一則，缺席日也算一則。

    缺席日必須進來——連續缺席代表系統有問題，而那是最該檢討的事。
    只收有決策的日子會造成倖存者偏差。
    """
    rows_by_date = {}
    for row in report.get("orders", []):
        rows_by_date.setdefault(row["date"], {})[(row["action"], row["code"])] = row
    adherence_by_date = {a["date"]: a
                         for a in report.get("adherence", {}).get("days", [])}
    fills_by_ref = {}
    for e in entries:
        if e.get("type") == "fill" and e.get("decision_ref") is not None:
            fills_by_ref.setdefault(e["decision_ref"], e)

    cases = []
    for e in entries:
        if not in_range(e.get("date", ""), since, until):
            continue
        if e.get("type") == "missed":
            cases.append({"date": e["date"], "ts": e.get("ts"), "missed": True,
                          "reason": e.get("reason")})
        elif e.get("type") == "decision":
            cases.append(_decision_case(e, fills_by_ref.get(e["date"]),
                                        rows_by_date.get(e["date"], {}),
                                        adherence_by_date.get(e["date"]),
                                        (contexts or {}).get(e["date"])))
    return sorted(cases, key=lambda c: c["date"])


def _decision_case(decision, fill_entry, order_rows, adherence_row, context=None):
    """一則決策案例＝當初怎麼想 ＋ 實際成交 ＋ 事後報酬 ＋ 紀律分。

    四者配在一起才看得出「想法」與「結果」的因果；分開看只是兩份無關的表。
    """
    orders = []
    for o in decision.get("orders", []):
        row = order_rows.get((o["action"], o["code"]), {})
        orders.append({**o,
                       "exec_price": row.get("exec_price"),
                       "exec_status": row.get("exec_status"),
                       "horizons": row.get("horizons", {})})
    return {
        "date": decision["date"],
        "ts": decision.get("ts"),
        "missed": False,
        "context": context,
        "rationale": decision.get("rationale", ""),
        "news_used": decision.get("news_used", []),
        "rules_applied": decision.get("rules_applied", []),
        "orders": orders,
        "unfilled": (fill_entry or {}).get("unfilled", []),
        "adherence": adherence_row,
        "amended": bool(decision.get("amended")),
        "violations": decision.get("violations", []),
    }


def rule_performance(cases, horizon):
    """依採用的準則分組算前瞻報酬——準則要能被自己的損益推翻。

    沒有這條歸因鏈，「依損益調整準則」就只是換一種方式憑感覺：改了規則卻
    無從得知改對沒有。尚未到期的單不計入分母，否則新準則會被當成 0% 報酬。

    委託帶自己的 `rules_applied` 時只歸給那幾條；沒有這個欄位（舊紀錄）才退回
    決策層的清單——那樣當天每筆單都記到當天每條準則頭上，各條數字會一模一樣。
    超額另外統計：報酬為正但落後基準的準則，不算被損益支持。
    """
    buckets = {}

    def slot(rule_id):
        return buckets.setdefault(rule_id, {"days": set(), "returns": [], "excess": []})

    for case in cases:
        day_rules = case.get("rules_applied") or []
        for rule_id in day_rules:
            slot(rule_id)["days"].add(case["date"])
        for o in case.get("orders", []):
            h = (o.get("horizons") or {}).get(horizon) or {}
            for rule_id in o["rules_applied"] if "rules_applied" in o else day_rules:
                s = slot(rule_id)
                s["days"].add(case["date"])
                if h.get("return_adj") is not None:
                    s["returns"].append(h["return_adj"])
                    if h.get("excess") is not None:
                        s["excess"].append(h["excess"])

    def mean(xs):
        return round(sum(xs) / len(xs), 6) if xs else None

    def share_positive(xs):
        return round(sum(1 for x in xs if x > 0) / len(xs), 4) if xs else None

    return {rule_id: {"n_decisions": len(s["days"]),
                      "n_evaluated": len(s["returns"]),
                      "avg_return": mean(s["returns"]),
                      "win_rate": share_positive(s["returns"]),
                      "avg_excess": mean(s["excess"]),
                      "excess_win_rate": share_positive(s["excess"])}
            for rule_id, s in buckets.items()}


# ---------------------------------------------------------------- 輸出

def exec_label(status, price):
    """成交狀態的人話。

    `no_fill` 的真正語意是「journal 裡還沒有這筆的成交紀錄」——當日複盤時多半
    只是還沒對回（成交對回要等隔日 settle），不等於沒成交。直接印 `no_fill`
    會被讀成「這單沒成交」，在交易系統裡是危險的誤讀。
    """
    if status == "filled":
        return f"成交 {price}"
    if status == "no_fill":
        return f"尚未對回成交紀錄（暫以決策日收盤 {price} 估算）"
    return "無法評估"


def _fmt_horizons(horizons):
    parts = []
    for n in evaluate.HORIZONS:
        h = horizons.get(n) or {}
        if h.get("return_adj") is not None:
            excess = h.get("excess")
            tail = f"（超額 {excess:+.2%}）" if excess is not None else ""
            parts.append(f"T+{n} {h['return_adj']:+.2%}{tail}")
        elif h.get("status") == "pending":
            parts.append(f"T+{n} 待期")
    return "｜".join(parts) or "尚無報酬"


def _render_context(ctx, orders):
    """決策當時的情境：方向、檔位、賣出優先序、跨檔候選比較。

    候選表標題寫「依位階金額」而非「依位階」——位階的權威定義還沒在 App 上
    驗證過（見專案 backlog），這裡只是照 tier_amount 排，不宣稱它就是位階序。
    """
    if not ctx:
        return []
    po = ctx["posture"]
    traded = {o["code"] for o in orders}
    gap = po.get("gap")
    out = [f"\n當時情境　建議持股 {po.get('suggested')}% vs 實際 {po.get('actual')}%"
           f"（gap {gap:+.2f}pp → {po.get('action') or '?'}）"
           f"｜主軌上限 {ctx['max_names']['effective']}"
           f"（App 給 {ctx['max_names']['app']}）"
           f"｜熔斷 {ctx.get('breaker')}"
           if gap is not None else "\n當時情境　posture 讀不到"]
    if ctx.get("sell_priority"):
        out.append(f"　　　　　賣出優先序 {'、'.join(ctx['sell_priority'])}"
                   f"｜可賣 {'、'.join(ctx.get('sellable') or [])}")
    if ctx.get("candidates"):
        out.append("　　　　　候選（依位階金額）：")
        for cand in ctx["candidates"]:
            mark = " ←本日交易" if cand["code"] in traded else ""
            out.append(f"　　　　　  {cand['code']:<8}"
                       f"{'+'.join(cand['tiers'] or []):<10}"
                       f"位階金額 {cand['tier_amount'] or 0:>6.0f}"
                       f"　折溢價 {cand['premium'] or 0:>6.2f}%{mark}")
    return out


def render(cases, perf, horizon):
    out = []
    for c in cases:
        if c.get("missed"):
            out.append(f"\n## {c['date']}　❌ 缺席")
            out.append(f"原因：{c.get('reason')}")
            continue
        flags = []
        if c["amended"]:
            flags.append("修訂過")
        if c["violations"]:
            flags.append(f"硬規則覆寫 {len(c['violations'])} 項")
        adherence = c.get("adherence") or {}
        score = adherence.get("score")
        out.append(f"\n## {c['date']}　紀律分 "
                   f"{f'{score:.0%}' if score is not None else '-'}"
                   + (f"　⚠️ {'、'.join(flags)}" if flags else ""))
        if c["rules_applied"]:
            out.append(f"採用準則：{'、'.join(c['rules_applied'])}")
        out += _render_context(c.get("context"), c["orders"])
        out.append(f"\n當時的判斷：{c['rationale']}")
        for o in c["orders"]:
            basis = o.get("ark_basis") or {}
            out.append(f"\n  {o['action']} {o['code']} {o['qty']} 股"
                       f"　[{o.get('track', 'core')}]"
                       f"　限價 {o.get('limit_low')}~{o.get('limit_high')}")
            out.append(f"    依據　{basis.get('signal')}={basis.get('value')}")
            if o.get("rules_applied"):
                out.append(f"    準則　{'、'.join(o['rules_applied'])}")
            out.append(f"    理由　{o.get('reason', '')}")
            out.append(f"    結果　{exec_label(o.get('exec_status'), o.get('exec_price'))}"
                       f"　{_fmt_horizons(o.get('horizons') or {})}")
        if c["unfilled"]:
            out.append(f"    未成交：{c['unfilled']}")
        failed = [k for k, v in (adherence.get("components") or {}).items() if not v]
        if failed:
            out.append(f"    紀律未過：{'、'.join(failed)}")

    if perf:
        out.append(f"\n\n## 準則損益歸因（T+{horizon}）")
        out.append("  準則      採用天數  已評筆數   勝率     平均報酬     平均超額  超額為正")

        def pct(v, spec):
            return format(v, spec) if v is not None else "-"

        for rule_id, p in sorted(perf.items()):
            out.append(f"  {rule_id:<10}{p['n_decisions']:>6}{p['n_evaluated']:>10}"
                       f"{pct(p['win_rate'], '.0%'):>8}{pct(p['avg_return'], '+.2%'):>12}"
                       f"{pct(p['avg_excess'], '+.2%'):>12}"
                       f"{pct(p['excess_win_rate'], '.0%'):>10}")
    else:
        out.append("\n\n（尚無決策標記採用準則——準則損益歸因需要 decision "
                   "帶 rules_applied 欄位）")
    return "\n".join(out)


def satellite_evaluated_line(report):
    """報告第一行：個股桶 T+5 已評筆數——複盤層據此決定閘門調不調。

    horizons 的鍵經過 JSON 往返會變字串，兩種都要讀得到。
    """
    horizons = (report.get("satellite") or {}).get("horizons") or {}
    bucket = horizons.get(5) or horizons.get("5") or {}
    return f"個股桶 T+5 已評 {bucket.get('evaluated', 0)} 筆（少於 5 筆時閘門維持不調）"


# ---------------------------------------------------------------- CLI

FETCH_MAX_FAILURES = 3     # 連續失敗幾檔就停手（Shioaji kbars 單次逾時 30 秒）


def fetch_prices(api, codes, start, end, fetch=None, load_cache=None,
                 max_failures=FETCH_MAX_FAILURES):
    """逐檔抓日K；失敗改用本地快取（篩在區間內）並記進缺口，連續失敗達上限後
    剩下的直接用快取、不再嘗試。回傳 (prices, gaps)。

    2026-10-02 的複盤因單一 kbars 逾時整支中斷。有界降級讓複盤照樣產出，
    但缺口要寫進報告——快取可能缺最新日，不能當成完整績效。
    """
    fetch = fetch or market.fetch_daily
    load_cache = load_cache or market.load_cache
    prices, gaps, streak = {}, {}, 0
    for code in codes:
        if streak >= max_failures:
            gaps[code] = f"未嘗試（前面已連續失敗 {streak} 檔）"
        else:
            try:
                prices[code] = fetch(api, code, start, end)
                streak = 0
                continue
            except Exception as e:      # 任何行情錯誤都降級，不讓整份複盤中斷
                streak += 1
                gaps[code] = f"抓取失敗：{e}"
        prices[code] = [b for b in load_cache(code) if start <= b["date"] <= end]
    return prices, gaps


def price_gaps_line(gaps):
    """報告開頭的缺口說明；沒有缺口回空字串。"""
    if not gaps:
        return ""
    detail = "；".join(f"{c} {why}" for c, why in sorted(gaps.items()))
    return (f"⚠️ 行情不完整：{len(gaps)} 檔改用本地快取（{detail}）。"
            "這些標的的前瞻報酬可能缺最新交易日；未到期者照列 pending，"
            "不可當成完整績效，也不足以改變準則狀態。")


def main():
    ap = argparse.ArgumentParser(description="決策複盤：組出逐筆案例供檢討")
    ap.add_argument("--since", help="起日（含），預設為 until 往前 7 天")
    ap.add_argument("--until", help="迄日（含），預設今天")
    ap.add_argument("--horizon", type=int, default=5,
                    choices=evaluate.HORIZONS, help="準則歸因用的前瞻視窗")
    ap.add_argument("--json", action="store_true", help="輸出機器可讀 JSON")
    ap.add_argument("--offline", action="store_true",
                    help="只用本地日K快取，不連 Shioaji 補資料")
    ap.add_argument("--out", help="把輸出寫到檔案（排程餵給複盤層用），不印到 stdout")
    args = ap.parse_args()

    until = args.until or dt.date.today().isoformat()
    since = args.since or default_window(until)[0]

    entries = journal.load_entries()
    packets = evaluate.load_all_packets()
    dividends = evaluate.load_dividends()
    if not any(e.get("type") == "decision" for e in entries):
        print("尚無任何決策紀錄")
        return 0

    codes = sorted({o["code"] for e in entries if e.get("type") == "decision"
                    for o in e.get("orders", [])} | {packet_mod.BENCHMARK})
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=7)).isoformat()
    today = dt.date.today().isoformat()
    gaps = {}
    if args.offline:
        prices = {c: [b for b in market.load_cache(c) if start <= b["date"] <= today]
                  for c in codes}
    else:
        try:
            with market.session() as api:
                prices, gaps = fetch_prices(api, codes, start, today)
        except Exception as e:          # 連登入都失敗：全部改用快取並標記
            prices, gaps = fetch_prices(None, codes, start, today, max_failures=0)
            gaps = {c: f"券商連線失敗：{e}" for c in gaps}

    report = evaluate.evaluate_all(entries, packets, prices,
                                   prices.get(packet_mod.BENCHMARK, []),
                                   dividends, orders_of=all_orders)
    contexts = {d: build_context(pk, load_envelope(d)) for d, pk in packets.items()}
    cases = cases_from(entries, report, since, until, contexts)
    perf = rule_performance(cases, args.horizon)
    if args.json:
        text = json.dumps({"since": since, "until": until, "cases": cases,
                           "rule_performance": perf, "price_gaps": gaps},
                          ensure_ascii=False, indent=2)
    else:
        text = ((price_gaps_line(gaps) + "\n\n" if gaps else "")
                + satellite_evaluated_line(report) + "\n\n"
                + f"複盤區間 {since} ～ {until}　案例 {len(cases)} 則\n"
                + render(cases, perf, args.horizon))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"✅ 複盤案例已存至 {args.out}（{len(cases)} 則）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
