# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = []
# ///
"""個股桶的閘門：起始值、允許區間、每次調整步幅，以及檔案讀寫的驗證。

閘門是複盤層可以動的數字（使用者 2026-09-08 指示），但區間與步幅是硬的——
防止複盤層在一段順風後把停損越放越鬆。要動區間得使用者親自改本檔。
"""
import json
import math
import os

DEFAULTS = {
    "name_cap": 0.12,            # 單檔市值上限（佔總資源）
    "sector_cap": 0.25,          # 同產業市值上限（佔總資源）
    "stop_loss": -0.12,          # 個股桶停損
    "trim_buffer": 0.03,         # 超過 name_cap 多少才列減碼候選（參考值）
    "share_of_invested": 0.5,    # 個股桶佔「投入金額」的比例
}

# (下界, 上界, 每次最多調整幅度)
BOUNDS = {
    "name_cap": (0.08, 0.15, 0.01),
    "sector_cap": (0.20, 0.35, 0.025),
    "stop_loss": (-0.15, -0.08, 0.01),
    "trim_buffer": (0.02, 0.05, 0.01),
    "share_of_invested": (0.30, 0.60, 0.05),
}
_EPS = 1e-9


def default_path():
    """`ARK_GATES`；否則跟著準則檔放（複盤層的自動放行只涵蓋準則檔所在目錄）。"""
    if os.environ.get("ARK_GATES"):
        return os.environ["ARK_GATES"]
    rules = os.environ.get("ARK_RULES")
    if rules:
        return os.path.join(os.path.dirname(rules), "gates.json")
    return os.path.expanduser("~/.ark-toolkit/gates.json")


def range_violations(g):
    out = []
    for key, (lo, hi, _step) in BOUNDS.items():
        if key not in g:
            continue
        v = g[key]
        # NaN 與所有數字比較都是 False，不另外擋就會靜默通過區間檢查
        if (not isinstance(v, (int, float)) or not math.isfinite(v)
                or v < lo - _EPS or v > hi + _EPS):
            out.append(f"{key}={v!r} 超出允許區間 [{lo}, {hi}]")
    return out


def step_violations(before, after):
    out = []
    for key, (_lo, _hi, step) in BOUNDS.items():
        if key in before and key in after and isinstance(before[key], (int, float)) and isinstance(after[key], (int, float)):
            if abs(after[key] - before[key]) > step + _EPS:
                out.append(f"{key} 由 {before[key]} 改為 {after[key]}，超過單次步幅 {step}")
    return out


def load(path=None):
    """讀閘門檔並與預設合併；區間外直接報錯，不靜默夾回。"""
    path = path or default_path()
    if not os.path.exists(path):
        return dict(DEFAULTS)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}：頂層必須是物件，不是 {type(data).__name__}")
    merged = {**DEFAULTS, **data}
    bad = range_violations(merged)
    if bad:
        raise ValueError(f"{path}：" + "；".join(bad))
    return merged


def intact(before_text, after_text):
    """複盤層改過 gates.json 之後的驗證：可解析、區間內、相對改前不超步幅。"""
    try:
        after = json.loads(after_text)
    except ValueError as e:
        return False, [f"gates.json 不是合法 JSON：{e}"]
    if not isinstance(after, dict):
        return False, ["gates.json 頂層必須是物件"]
    problems = range_violations({**DEFAULTS, **after})
    if before_text.strip():
        try:
            before = json.loads(before_text)
            if isinstance(before, dict):
                problems += step_violations({**DEFAULTS, **before}, {**DEFAULTS, **after})
        except ValueError:
            pass
    return not problems, problems
