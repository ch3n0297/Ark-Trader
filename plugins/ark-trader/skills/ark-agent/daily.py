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
"""每日排程編排：順序寫死、不可跳過。

    daily.py decide    平日 10:00　事實 → 風控 → 判斷 → 紀律驗證 → 送單
    daily.py settle    平日 14:30　成交對回 → 淨值 → 同步庫存與現金欄
    daily.py trade     隨選（盤中）對帳統一 → decide 的五步 → 等成交 → 當日結算；
                       給不用排程、由人說「開始交易」才動的使用者
    daily.py review    週五 15:00　本週案例 → 檢討並更新準則 → 驗準則檔 → commit
    daily.py check     隨選　　　　前置檢查（無副作用，只讀）

**LLM 只佔 decide 的第 3 步，而且不給 Bash 工具**（`ARK_DECISION_CLI=codex` 時改走
`codex exec` 的唯讀沙箱）——它產出 decision.json 之後就沒有話語權，紀律驗證與
送單由後面兩步負責。若讓 LLM 自己編排整條流程，它可能「忘記」跑風控或驗證，
硬規則就不再是硬的。

**為什麼是 Python 而不是 shell**：macOS 刻意封鎖系統二進位檔（`/bin/bash`）在
launchd 下對 `~/Documents` 等受保護目錄的存取，以防有人用腳本繞過 TCC。實測
`/bin/bash` 連讀取本檔都會 `Operation not permitted`，而 `uv` 這類第三方 binary
有自己的 TCC 身分、讀得到。所以 launchd 的進入點必須是 `uv run daily.py`。

送單環境由 ARK_BACKEND 決定，**預設 simulation**：切實單要明確改設定，
不會因為忘記帶參數就動到真錢。
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.ark-toolkit/agent")
LOG = os.path.join(STATE, "daily.log")
SYNC = os.path.abspath(os.path.join(HERE, "..", "ark-sync", "sync.py"))
# 無人值守：config 缺均價口徑時 sync 會開視窗詢問，排程沒人按會卡死，一定帶 --no-prompt
SYNC_ARGS = ("--allow-delete", "--with-cash", "--no-prompt")

PROCEED, SKIP, FAIL = "proceed", "skip", "fail"

# (排定時刻, 容許分鐘)。decide 窗窄——盤中判斷過了時間就失效；settle 窗寬——
# 那是對既成事實的記錄，晚幾小時做結果一樣。
WINDOWS = {"decide": ("10:00", 60), "settle": ("14:30", 240),
           "review": ("15:00", 240)}

# 手動一場（trade）的開始時段：不看排定時刻，只看還來不來得及在 13:30 收盤前
# 等單（預設 30 分）＋結算。
TRADE_WINDOW = ("09:00", "13:00")

MODES = ("decide", "settle", "trade", "review", "check")

# packet.py 的離開碼 3＝休市（國定假日）：今天不交易，不是故障（值與 packet.MARKET_CLOSED_EXIT 相同）
PACKET_MARKET_CLOSED = 3

# 決策層用哪支模型 CLI：claude（預設，`claude -p`）或 codex（`codex exec`，唯讀沙箱）。
# 只管 decide／trade 的第 3 步；週複盤要改準則檔，固定走 claude（見 review）。
CLAUDE, CODEX = "claude", "codex"
DECISION_CLIS = (CLAUDE, CODEX)

# claude 的預設模型。無人值守下要動真錢，判斷品質是最不該省的地方。
CLAUDE_MODEL = "fable"


def model_settings(env):
    """環境變數 → (決策 CLI, 決策模型, 複盤模型)。

    ARK_DECISION_CLI 未設或空白＝claude（不認得的值原樣回傳，由 main 擋下）。
    決策模型有設 ARK_DECISION_MODEL 就用；沒設時 claude 用 fable，codex 為 None
    （不帶 -m，用 Codex 自己的預設模型——claude 的模型名對 codex 沒有意義）。
    複盤固定走 claude：決策層切到 codex 時 ARK_DECISION_MODEL 是 codex 的模型名，
    不能拿來用，改回 claude 的預設。
    """
    cli = env.get("ARK_DECISION_CLI", "").strip() or CLAUDE
    configured = env.get("ARK_DECISION_MODEL", "").strip()
    model = configured or (CLAUDE_MODEL if cli == CLAUDE else None)
    review = model if cli == CLAUDE else CLAUDE_MODEL
    return cli, model, review


DECISION_CLI, DECISION_MODEL, REVIEW_MODEL = model_settings(os.environ)

# 主模型撞到用量上限時的接手模型。額度是跟著模型走的，撞牆那天會整天不交易，
# 實驗數據就多一個與市場無關的缺口——換一個額度池把當天補回來。
# 只用在 claude：額度判斷認的是 claude CLI 的訊息（見 is_quota_exhausted）。
FALLBACK_MODEL = os.environ.get("ARK_DECISION_FALLBACK", "opus")


# ---------------------------------------------------------------- 純邏輯

def is_trading_weekday(date):
    """週末不跑。國定假日不在這裡擋——settle 以 0050 當日有無行情判定休市，
    比維護一份會過期的假日表可靠。"""
    return dt.date.fromisoformat(date).weekday() < 5


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_within_window(now, scheduled, tolerance_minutes):
    """現在時間是否落在排定時段的容許範圍內（"HH:MM" 格式，含端點）。

    launchd 對錯過的 StartCalendarInterval 會在系統喚醒後補跑一次——機器若在
    10:00 睡著，13:00 醒來就會執行，而那時盤中價格早已不同、限價全部失效。
    時間窗必須由程式自己守，不能寄望排程器的語意。
    """
    return abs(_minutes(now) - _minutes(scheduled)) <= tolerance_minutes


def is_within_range(now, start, end):
    """現在時間是否落在 [start, end]（"HH:MM"，含端點）。"""
    return _minutes(start) <= _minutes(now) <= _minutes(end)


def has_decision(entries, date):
    """當日是否已有決策。有就不再跑一場——execute 送的是當日第一筆決策，
    再跑等於把同一批單再送一次。"""
    import journal
    return journal.first_decision(entries, date) is not None


def classify_risk_exit(code):
    """risk.py 的離開碼：0 繼續、3 今日完全不能動、其餘為故障。

    混為一談的話，全面熔斷會被誤報成系統故障，反過來也一樣危險。
    """
    return {0: PROCEED, 3: SKIP}.get(code, FAIL)


def _render(template, mapping):
    """把提示模板的佔位符換成實際路徑。

    換完仍殘留 `*_PATH` 字樣就報錯：Agent 會照字面把決策寫到 'DECISION_PATH'
    這個檔名，排程接著找不到檔案，而那時已經浪費一次決策機會了。
    """
    out = template
    for key, value in mapping.items():
        out = out.replace(key, value)
    left = re.findall(r"\b[A-Z_]+_PATH\b", out)
    if left:
        raise ValueError(f"提示模板有未取代的佔位符：{sorted(set(left))}")
    return out


def render_prompt(template, packet, envelope, decision, date):
    return _render(template, {"PACKET_PATH": packet, "ENVELOPE_PATH": envelope,
                              "DECISION_PATH": decision, "TODAY": date})


def render_review_prompt(template, review, rules, doc, date, gates_path=""):
    return _render(template, {"REVIEW_PATH": review, "RULES_PATH": rules,
                              "REVIEW_DOC_PATH": doc, "TODAY": date,
                              "GATES_PATH": gates_path})


def guard_gates(path, before_text, mode):
    """複盤後驗 gates.json：區間外、超步幅或壞 JSON 就從備份還原。回傳是否還原過。"""
    import gates
    if not os.path.exists(path):
        return False
    ok, why = gates.intact(before_text, _read(path))
    if ok:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(before_text if before_text.strip() else json.dumps(gates.DEFAULTS, indent=2))
    log(mode, "⚠️ gates.json 被改到區間或步幅之外，已還原：" + "；".join(why))
    return True


def review_doc_path(rules_path, date):
    """檢討記錄放在準則檔旁的 decision-reviews/——準則與它的「為什麼」一起版控。"""
    return os.path.join(os.path.dirname(rules_path), "decision-reviews", f"{date}.md")


# 成功紀錄機制上線日。這天之前的檢討檔（YYYY-MM-DD.md）視為已完成的複盤（保守兼容）；
# 之後只有 reviews/completed.jsonl 的成功紀錄算——複盤層可能先寫出檢討檔，之後才因
# 非零離開或準則被改壞而失敗，檔案還在，不能據此推進下次的複盤起日。
REVIEW_RECORD_START = "2026-10-08"


def _is_iso_date(text):
    try:
        dt.date.fromisoformat(text)
        return True
    except (TypeError, ValueError):
        return False


def completed_reviews(records, doc_names, legacy_before=REVIEW_RECORD_START):
    """已完成複盤的迄日（排序、去重）：成功紀錄 ∪ 上線前的檢討檔。"""
    done = {r["until"] for r in records
            if isinstance(r, dict) and r.get("ok") is True and _is_iso_date(r.get("until"))}
    done |= {n[:-3] for n in doc_names
             if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", n) and _is_iso_date(n[:-3])
             and n[:-3] < legacy_before}
    return sorted(done)


def review_since(done_dates, until, default_days=7, max_days=28):
    """複盤起日：上次成功複盤迄日的隔天，最多回看 max_days 天。

    不比預設 7 天視窗短時回 None（照 review.py 預設）。某次複盤失敗（例如 kbars
    逾時），那幾天的案例才不會永遠掉出視窗。
    """
    done = [d for d in done_dates if d < until]
    if not done:
        return None
    end = dt.date.fromisoformat(until)
    since = max(dt.date.fromisoformat(max(done)) + dt.timedelta(days=1),
                end - dt.timedelta(days=max_days - 1))
    return since.isoformat() if since < end - dt.timedelta(days=default_days - 1) else None


def rules_intact(before, after):
    """模型改過的準則檔是否仍完整：原有編號一條不少（淘汰是搬到「已淘汰」段，
    不是刪掉）。編號消失或標題格式被改到解析不到，隔天決策就拿不到準則，而且
    不會有人發現——寫回前要擋。原本就沒準則的新裝環境，寫不寫都不算改壞。"""
    from packet import rule_ids
    old = rule_ids(before)
    return not old or set(old) <= set(rule_ids(after))


def is_quota_exhausted(output):
    """claude CLI 是否因用量上限而離開。

    只認額度訊息獨有的 `/usage-credits` 指引——把所有非零離開都當額度問題的話，
    prompt 寫錯或網路斷線也會換個模型再燒一次決策。
    """
    return "/usage-credits" in output


def fallback_model(code, output, current, fallback):
    """該不該換模型重試；要換就回傳接手模型，否則 None。

    降級只在額度這一種失敗上成立：其餘故障換模型也是一樣的結果。
    接手模型與現用相同時不重試——那是拿同一個已耗盡的額度池再撞一次。
    """
    if code == 0 or not fallback or current == fallback:
        return None
    return fallback if is_quota_exhausted(output) else None


# codex 的決策層在唯讀沙箱裡（寫不了檔、指令連不了網），決策 JSON 改由最終訊息交回、
# daily.py 代寫到 DECISION_PATH。接在 decide.md 之後，蓋掉「寫到檔案」那段的作法。
CODEX_DECISION_SUFFIX = """

## 交回決策的方式（覆寫上面「寫出決策」的作法）

你在唯讀沙箱裡，**不能寫檔**，也不能、不需要送出任何委託——紀律驗證與送單由後面的
腳本負責。上面要你寫到 `{decision}` 的那份決策 JSON，改成直接當作你的**最終訊息**
輸出：只輸出那一個 JSON 物件本身，格式與欄位照上面的範例，前後不要加任何說明文字，
也不要包在 code fence 裡。排程程式會把它寫進檔案。
"""


def model_command(cli, prompt, model, tools):
    """組模型 CLI 的指令列。兩種 CLI 各一個分支，都只讓模型「讀資料、產出決策」：

    - claude：`claude -p`，只給 `tools`（不含 Bash），由模型用 Write 寫決策檔。
    - codex：`codex exec`，**固定唯讀沙箱**（模型產生的指令寫不了檔、連不了網，送不了單），
      stdout 只有最終訊息（進度在 stderr）。`--ignore-user-config` 不讀
      `$CODEX_HOME/config.toml`——排程結果不隨個人設定（沙箱、核准、模型…）漂移；
      排程目錄不是 git repo，要 `--skip-git-repo-check`；新聞要即時的，
      `web_search` 設 live（預設是 cached）。不讀個人設定時推理強度會落到 none，
      無人值守下動真錢的判斷最不該省，寫死 xhigh。`model` 為 None 就不帶 `-m`。

    codex 只接決策層：複盤要改準則檔，給它寫入權限就打破「決策層唯讀」這條不變量。
    """
    if cli == CLAUDE:
        return ["claude", "-p", prompt,
                "--model", model,
                "--allowedTools", *tools,
                "--permission-mode", "acceptEdits",
                "--output-format", "text"]
    if cli == CODEX:
        if any(t in tools for t in ("Edit", "Bash")):
            raise ValueError("codex 只接唯讀的決策層，不給寫入或 Bash 類工具")
        cmd = ["codex", "exec",
               "--sandbox", "read-only",
               "--ignore-user-config",
               "--skip-git-repo-check",
               "--color", "never",
               "-c", 'web_search="live"',
               "-c", 'model_reasoning_effort="xhigh"']
        if model:
            cmd += ["-m", model]
        return cmd + [prompt]
    raise ValueError(f"未知的決策 CLI：{cli!r}（可用 {'、'.join(DECISION_CLIS)}）")


def parse_decision_output(text):
    """codex 最終訊息 → 決策 dict。容忍整段被包在 ```json fence 裡；其他雜訊一律不認——
    猜著從一段話裡挖 JSON，挖錯了就是一筆不是模型本意的決策。格式與紀律由 journal 驗。"""
    body = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n?```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    try:
        obj = json.loads(body)
    except ValueError as exc:
        raise ValueError(f"決策輸出不是 JSON：{exc}") from None
    if not isinstance(obj, dict):
        raise ValueError("決策輸出不是 JSON 物件")
    return obj


def required_clis(cli):
    """前置檢查要找的 CLI：(缺了就不過, 缺了只提醒)。codex 模式的決策層不用 claude，
    但週複盤仍走 claude——缺了只影響週五，不該擋掉每天的決策。"""
    if cli == CODEX:
        return [CODEX], [CLAUDE]
    return [CLAUDE], []


def settle_failures(results):
    """[(步驟名, 離開碼)] → 失敗的步驟名。

    結算每一步都要盡力做完再回報：成交對回失敗不該讓淨值記錄也跳過，
    否則熔斷基準會斷掉。
    """
    return [name for name, code in results if code != 0]


# ---------------------------------------------------------------- 副作用

def in_window(mode):
    """現在是否還在該模式的執行時窗內。逾時就放棄——補跑一個過期的決策
    比不跑危險得多。`ARK_FORCE_WINDOW=1` 可略過（供手動測試）。"""
    if os.environ.get("ARK_FORCE_WINDOW"):
        return True
    scheduled, tolerance = WINDOWS[mode]
    now = dt.datetime.now().strftime("%H:%M")
    if is_within_window(now, scheduled, tolerance):
        return True
    notify(mode, f"已逾時不執行：現在 {now}，排定 {scheduled}"
                 f"（容許 ±{tolerance} 分）")
    return False


def in_trade_window():
    """手動一場是否還來得及開始。`ARK_FORCE_WINDOW=1` 可略過（供手動測試）。"""
    if os.environ.get("ARK_FORCE_WINDOW"):
        return True
    now = dt.datetime.now().strftime("%H:%M")
    if is_within_range(now, *TRADE_WINDOW):
        return True
    notify("trade", f"不在交易窗內不執行：現在 {now}，"
                    f"交易窗 {TRADE_WINDOW[0]}–{TRADE_WINDOW[1]}")
    return False


def load_journal():
    import journal
    return journal.load_entries()


def log(mode, message):
    os.makedirs(STATE, exist_ok=True)
    stamp = dt.datetime.now().replace(microsecond=0).isoformat(" ")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} [{mode}] {message}\n")


def notify(mode, message):
    log(mode, message)
    print(message, flush=True)
    subprocess.run(["osascript", "-e",
                    f'display notification {message.replace(chr(34), chr(39))!r} '
                    f'with title "ark-agent · {mode}"'],
                   capture_output=True, check=False)


def run(mode, *args):
    """跑一支子腳本，輸出接進 daily.log，回傳離開碼。

    用 sys.executable 而非巢狀 uv run：本檔的 PEP 723 已宣告全部依賴，
    子腳本共用同一個直譯器，省下每步重新解析環境的時間。
    """
    log(mode, "$ " + " ".join(str(a) for a in args))
    with open(LOG, "a", encoding="utf-8") as fh:
        return subprocess.run([sys.executable, *[str(a) for a in args]],
                              stdout=fh, stderr=subprocess.STDOUT,
                              check=False).returncode


DECIDE_TOOLS = ("Read", "Write", "WebSearch", "WebFetch")
REVIEW_TOOLS = ("Read", "Write", "Edit")       # 改準則檔要 Edit；複盤不上網


def ask_model(mode, prompt, model, tools=DECIDE_TOOLS, cwd=STATE, cli=CLAUDE):
    """LLM 插槽：只給讀檔／寫檔／查新聞，**不給 Bash**——它無法自行送單或改紀錄
    （codex 則是唯讀沙箱，見 model_command）。回傳 (離開碼, 輸出)，輸出同時進 daily.log。

    先收進記憶體再寫檔而非直接導向 log：額度耗盡與其他故障都是非零離開，
    要分辨只能看輸出內容。codex 的 stdout 只有最終訊息（決策 JSON），進度在
    stderr——兩者分開收，回傳的輸出只取 stdout。

    stdin 一律接 /dev/null：`codex exec` 見到非 TTY 的 stdin 會讀到 EOF 才開始
    （把內容附加到提示後），繼承到一條不會關的 pipe／socket 就永遠卡住。
    """
    cmd = model_command(cli, prompt, model, tools)
    log(mode, f"呼叫 {cli}（{model or '預設模型'}）…")
    split = cli == CODEX
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if split else subprocess.STDOUT,
            text=True, cwd=cwd, check=False)
    except FileNotFoundError:
        log(mode, f"🛑 找不到 {cli} CLI")
        return 127, ""
    with open(LOG, "a", encoding="utf-8") as fh:
        if split and proc.stderr:
            fh.write(proc.stderr)
        fh.write(proc.stdout)
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------- 模式

def decide(date, backend, execute=True):
    mode = "decide"
    if not is_trading_weekday(date):
        log(mode, "週末，不執行")
        return 0
    if not in_window(mode):
        return 0
    return _decide_steps(mode, date, backend, execute, None)


def _decide_steps(mode, date, backend, execute, wait_minutes):
    """事實 → 風控 → 判斷 → 紀律驗證 → 送單。decide 與 trade 共用；
    `wait_minutes` 非空時送單後留在線上等成交（逾時刪單）。"""
    rc = run(mode, os.path.join(HERE, "packet.py"))
    if rc == PACKET_MARKET_CLOSED:
        notify(mode, f"{date} 休市，今日不交易（基準無當日行情，不拿舊行情做決策）")
        return 0
    if rc != 0:
        notify(mode, "🛑 決策包產生失敗")
        return 1

    verdict = classify_risk_exit(run(mode, os.path.join(HERE, "risk.py")))
    if verdict == SKIP:
        notify(mode, "今日不交易：執行邊界全面阻擋（詳見 daily.log）")
        return 0
    if verdict == FAIL:
        notify(mode, "🛑 風控邊界產生失敗")
        return 1

    packet = os.path.join(STATE, "packets", f"{date}.json")
    envelope = os.path.join(STATE, "envelopes", f"{date}.json")
    decision = os.path.join(STATE, "decisions", f"{date}.json")
    if not (os.path.exists(packet) and os.path.exists(envelope)):
        notify(mode, "🛑 決策包或執行邊界不存在")
        return 1
    os.makedirs(os.path.dirname(decision), exist_ok=True)

    with open(os.path.join(HERE, "prompts", "decide.md"), encoding="utf-8") as fh:
        prompt = render_prompt(fh.read(), packet, envelope, decision, date)

    cli = DECISION_CLI
    if cli == CODEX:
        prompt += CODEX_DECISION_SUFFIX.format(decision=decision)

    rc, out = ask_model(mode, prompt, DECISION_MODEL, cli=cli)
    successor = fallback_model(rc, out, DECISION_MODEL, FALLBACK_MODEL) if cli == CLAUDE else None
    if successor:
        notify(mode, f"⚠️ {DECISION_MODEL} 額度耗盡，改用 {successor} 重試")
        rc, out = ask_model(mode, prompt, successor, cli=cli)
    if rc != 0:
        notify(mode, f"🛑 決策產生失敗（{cli} 非零離開）")
        return 1
    if cli == CODEX:
        # 解析失敗就在這裡停：當天若有舊的決策檔，不能讓它冒充這次的輸出
        try:
            parsed = parse_decision_output(out)
        except ValueError as exc:
            notify(mode, f"🛑 {exc}（未送出任何委託）")
            return 1
        with open(decision, "w", encoding="utf-8") as fh:
            json.dump(parsed, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    if not os.path.exists(decision):
        notify(mode, f"🛑 決策檔未產生：{decision}")
        return 1

    if run(mode, os.path.join(HERE, "journal.py"), "record", decision) != 0:
        notify(mode, "🛑 決策違反 ARK 紀律，已拒絕寫入（未送出任何委託）")
        return 1
    if not execute:
        notify(mode, f"✅ {date} 決策已鎖定，依指示不送單（改走人工／盤後通道）")
        return 0
    wait = ("--wait", str(wait_minutes)) if wait_minutes else ()
    if run(mode, os.path.join(HERE, "execute.py"), "--backend", backend, *wait) != 0:
        notify(mode, f"🛑 送單未全部成功（{backend}）——請查 daily.log 與 journal")
        return 1

    notify(mode, f"✅ {date} 決策完成並送出（{backend}）")
    return 0


def settle(date):
    mode = "settle"
    if not is_trading_weekday(date):
        log(mode, "週末，不執行")
        return 0
    if not in_window(mode):
        return 0
    failed = settle_failures(_settle_steps(mode))
    if failed:
        notify(mode, f"⚠️ {date} 結算部分失敗：{'、'.join(failed)}")
        return 1
    notify(mode, f"✅ {date} 盤後結算完成")
    return 0


def _settle_steps(mode, same_day=False):
    """成交對回 → 淨值 → 除權息 → 同步 ARK → 實現收益；每步盡力做完再回報。
    `same_day` 結算今天的決策（trade 成交後立刻結算），否則結算前一交易日。"""
    # 兩個寫 ARK App 的步驟排在最後且相鄰：AX 操作最脆弱，先把純資料的記錄
    # 做完，App 出問題也不會連累成交對回與淨值——那兩者是熔斷基準的來源。
    settle_args = ("settle", "--same-day") if same_day else ("settle",)
    return [
        ("成交對回", run(mode, os.path.join(HERE, "journal.py"), *settle_args)),
        ("淨值記錄", run(mode, os.path.join(HERE, "equity.py"))),
        # 除權息預告表只含尚未發生的事件，除息日一過就從表上消失——必須每天
        # 抓、累積存檔，等前瞻報酬到期時資料才在。放在寫 App 之前：純網路取數，
        # 失敗也不影響後面兩步。
        ("除權息資料", run(mode, os.path.join(HERE, "dividends.py"))),
        ("庫存/現金同步", run(mode, SYNC, *SYNC_ARGS)),
        ("實現收益記錄", run(mode, os.path.join(HERE, "record_return.py"))),
    ]


def trade(date, backend, wait_minutes=30):
    """手動一場：對帳統一 → 決策送單（等成交，逾時刪單）→ 當日結算。

    與排程的 decide／settle 差在兩點：不看排定時刻，只看還來不來得及在收盤前
    等單＋結算；成交後立刻結算當日，不留到隔天——ARK 庫存、淨值點與實現收益
    在同一場內對齊，下一場的第一步再對帳時才會是「一致」。
    """
    mode = "trade"
    if not is_trading_weekday(date):
        log(mode, "週末，不執行")
        return 0
    if not in_trade_window():
        return 0
    if has_decision(load_journal(), date):
        notify(mode, f"🛑 {date} 已有決策，不重複交易（同日只認第一筆）")
        return 1
    if run(mode, SYNC, *SYNC_ARGS) != 0:
        notify(mode, "🛑 ARK 與券商對帳同步失敗，不進決策——紀律邊界會建立在錯的持倉上")
        return 1
    rc = _decide_steps(mode, date, backend, True, wait_minutes)
    if rc != 0:
        return rc
    if not has_decision(load_journal(), date):
        return 0                    # 執行邊界全面阻擋、沒做決策，無事可結
    failed = settle_failures(_settle_steps(mode, same_day=True))
    if failed:
        notify(mode, f"⚠️ {date} 交易已送出，但當日結算部分失敗：{'、'.join(failed)}")
        return 1
    notify(mode, f"✅ {date} 一場完成：對帳 → 決策送單 → 成交 → 結算（{backend}）")
    return 0


def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def commit_files(mode, paths, message):
    """把複盤產物 commit 進準則檔所在的 repo。不在 repo 內或 git 失敗只記 log——
    準則已在磁碟上生效，commit 是追溯用，不該讓它擋住複盤。"""
    top = subprocess.run(["git", "-C", os.path.dirname(paths[0]),
                          "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False)
    if top.returncode != 0:
        log(mode, "準則檔不在 git repo 內，略過 commit")
        return False
    root = top.stdout.strip()
    for args in (["add", "--", *paths], ["commit", "-m", message, "--", *paths]):
        log(mode, "$ git " + " ".join(args))
        with open(LOG, "a", encoding="utf-8") as fh:
            rc = subprocess.run(["git", "-C", root, *args], stdout=fh,
                                stderr=subprocess.STDOUT, check=False).returncode
        if rc != 0:
            log(mode, "⚠️ git 失敗，準則已生效但未 commit（可稍後手動補）")
            return False
    return True


def review_records_path():
    return os.path.join(STATE, "reviews", "completed.jsonl")


def load_review_records(path=None):
    """複盤成功紀錄，壞行略過（比照 journal.load_entries）。"""
    path = path or review_records_path()
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


def append_review_record(record, path=None):
    path = path or review_records_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def review(date):
    """週複盤：本週案例 → 模型檢討並更新準則 → 驗準則檔 → commit → 通知。

    模型只拿到 Read／Write／Edit，改的是準則檔與檢討記錄；改壞（編號消失）就
    從備份還原，隔天決策拿到的永遠是解析得到的準則。
    """
    import packet as packet_mod
    mode = "review"
    if not in_window(mode):
        return 0
    rules = packet_mod.RULES_PATH
    reviews = os.path.join(STATE, "reviews")
    os.makedirs(reviews, exist_ok=True)
    report = os.path.join(reviews, f"{date}.txt")
    doc_dir = os.path.dirname(review_doc_path(rules, date))
    since = review_since(completed_reviews(
        load_review_records(), os.listdir(doc_dir) if os.path.isdir(doc_dir) else []), date)
    window = ("--since", since) if since else ()
    if run(mode, os.path.join(HERE, "review.py"), "--until", date, *window,
           "--out", report) != 0:
        notify(mode, "🛑 複盤案例產生失敗")
        return 1

    before = _read(rules)
    with open(os.path.join(reviews, f"{date}.rules.bak"), "w", encoding="utf-8") as fh:
        fh.write(before)
    import gates as gates_mod
    gates_path = gates_mod.default_path()
    gates_before = _read(gates_path) if os.path.exists(gates_path) else ""
    if gates_before:
        with open(os.path.join(reviews, f"{date}.gates.bak"), "w", encoding="utf-8") as fh:
            fh.write(gates_before)
    doc = review_doc_path(rules, date)
    os.makedirs(os.path.dirname(doc), exist_ok=True)
    with open(os.path.join(HERE, "prompts", "review.md"), encoding="utf-8") as fh:
        prompt = render_review_prompt(fh.read(), report, rules, doc, date, gates_path=gates_path)

    cwd = os.path.dirname(rules)          # acceptEdits 只自動放行工作目錄內的改動
    # 複盤固定走 claude（REVIEW_MODEL）：要 Edit 準則檔，codex 的唯讀沙箱做不到
    rc, out = ask_model(mode, prompt, REVIEW_MODEL, tools=REVIEW_TOOLS, cwd=cwd)
    successor = fallback_model(rc, out, REVIEW_MODEL, FALLBACK_MODEL)
    if successor:
        notify(mode, f"⚠️ {REVIEW_MODEL} 額度耗盡，改用 {successor} 重試")
        rc, out = ask_model(mode, prompt, successor, tools=REVIEW_TOOLS, cwd=cwd)
    # 先驗閘門：模型跑完就可能已經改壞 gates.json，任何提前 return 都不能跳過還原
    if guard_gates(gates_path, gates_before, mode):
        notify(mode, "⚠️ 閘門檔被改到區間外，已還原；準則檔照常")
    if rc != 0:
        notify(mode, "🛑 複盤失敗（claude 非零離開）")
        return 1

    if not rules_intact(before, _read(rules)):
        with open(rules, "w", encoding="utf-8") as fh:
            fh.write(before)
        notify(mode, "🛑 準則檔被改壞（編號消失或格式解析不到），已從備份還原")
        return 1
    if not os.path.exists(doc):
        notify(mode, f"🛑 檢討記錄未產生：{doc}")
        return 1
    commit_files(mode, [rules, doc] + ([gates_path] if os.path.exists(gates_path) else []),
                f"決策複盤 {date}")
    # 只有走到這裡（模型零離開、準則完整、檢討檔存在）才算完成。commit 失敗不影響：
    # 準則已在磁碟上生效，commit 只是追溯用（見 commit_files）。
    append_review_record({"until": date, "since": since, "ok": True, "doc": doc,
                          "ts": dt.datetime.now().replace(microsecond=0).isoformat()})
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    notify(mode, f"✅ {date} 複盤完成：{lines[-1][:80] if lines else ''}")
    return 0


def has_cli(name):
    return subprocess.run(["which", name], capture_output=True, check=False).returncode == 0


def check(backend):
    mode = "check"
    bad = []
    if subprocess.run(["pgrep", "-x", "Arkeration"],
                      capture_output=True, check=False).returncode != 0:
        bad.append("方舟運算 App 未執行")
    required, optional = required_clis(DECISION_CLI)
    for name in required:
        if not has_cli(name):
            bad.append(f"找不到 {name}（ARK_DECISION_CLI={DECISION_CLI}）")
    for name in optional:
        if not has_cli(name):
            log(mode, f"⚠️ 找不到 {name}：決策層不受影響，但週複盤（review）仍需 {name}")
    if run(mode, os.path.join(HERE, "preflight.py")) != 0:
        bad.append("preflight 未過（詳見 daily.log）")
    if bad:
        notify(mode, f"🛑 前置檢查未過：{'、'.join(bad)}")
        return 1
    notify(mode, f"✅ 前置檢查全過（backend={backend}）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="ark-agent 每日排程編排")
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--date", help="覆寫日期（預設今天）")
    ap.add_argument("--no-execute", action="store_true",
                    help="decide 只做到鎖定決策，不送單（盤中來不及時改走盤後零股）")
    ap.add_argument("--wait", type=float, default=30, metavar="MINUTES",
                    help="trade：送單後最多等幾分鐘成交，逾時刪單再結算（預設 30）")
    args = ap.parse_args()

    date = args.date or dt.date.today().isoformat()
    backend = os.environ.get("ARK_BACKEND", "simulation")
    if args.mode in ("decide", "trade", "check") and DECISION_CLI not in DECISION_CLIS:
        # 在任何一步之前擋：trade 的第一步就會寫 ARK App，設定錯不該走到那裡
        notify(args.mode, f"🛑 ARK_DECISION_CLI 只能是 {'、'.join(DECISION_CLIS)}，"
                          f"現在是 {DECISION_CLI!r}")
        return 1
    if args.mode == "decide":
        return decide(date, backend, execute=not args.no_execute)
    if args.mode == "settle":
        return settle(date)
    if args.mode == "trade":
        return trade(date, backend, args.wait)
    if args.mode == "review":
        return review(date)
    return check(backend)


if __name__ == "__main__":
    sys.exit(main())
