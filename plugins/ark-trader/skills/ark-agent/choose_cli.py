#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""以 macOS 原生視窗選決策層 CLI 與模型，寫進 ~/.ark-toolkit/agent.env。

install.sh 初次安裝時以 `--if-missing` 呼叫（agent.env 已設 ARK_DECISION_CLI
就不打擾）；`install.sh decision` 隨時重選。推理強度不在這裡選：codex 寫死
xhigh（daily.model_command）。按取消一律不改檔。改完若已裝排程，要重跑
`install.sh schedule` 讓 plist 跟上。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

CLIS = ("claude", "codex")
CODEX_DEFAULT = "Codex 預設模型"
# 每支 CLI 的第一個是預設選項
MODELS = {"claude": ["fable", "opus"], "codex": ["gpt-6-astra", CODEX_DEFAULT]}
ENV_PATH = os.path.expanduser("~/.ark-toolkit/agent.env")
_KEY = re.compile(r"^\s*(?:export\s+)?(ARK_DECISION_CLI|ARK_DECISION_MODEL)\s*=\s*(.*)$")


def current_cli(text):
    """agent.env 目前的 ARK_DECISION_CLI；未設回 None。"""
    for line in text.splitlines():
        m = _KEY.match(line)
        if m and m.group(1) == "ARK_DECISION_CLI":
            return m.group(2).strip().strip("'\"") or None
    return None


def update_env(text, cli, model):
    """換掉決策層兩個鍵、其他行原樣保留；model 為 None 時不寫模型（用 CLI 自己的預設）。"""
    kept = [l for l in text.splitlines() if not _KEY.match(l)]
    new = [f"ARK_DECISION_CLI={cli}"] + ([f"ARK_DECISION_MODEL={model}"] if model else [])
    return "\n".join(kept + new) + "\n"


def available_clis(which=shutil.which):
    return [c for c in CLIS if which(c)]


def model_value(choice):
    return None if choice == CODEX_DEFAULT else choice


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def list_script(prompt, items, default):
    return (f"choose from list {{{', '.join(_q(i) for i in items)}}} "
            f"with title {_q('ark-trader 決策層')} with prompt {_q(prompt)} "
            f"default items {{{_q(default)}}}")


def parse_choice(out):
    """osascript 的輸出；按取消時是 false。"""
    s = out.strip()
    return None if not s or s == "false" else s


def ask_dialog(prompt, items, default):
    done = subprocess.run(["osascript", "-e", list_script(prompt, items, default)],
                          capture_output=True, text=True, check=False)
    return parse_choice(done.stdout) if done.returncode == 0 else None


def run(text, ask=ask_dialog, which=shutil.which, if_missing=False):
    """回傳新的 agent.env 內容；不需要改（已設定、沒裝 CLI、按取消）回 None。"""
    if if_missing and current_cli(text):
        return None
    clis = available_clis(which)
    if not clis:
        return None
    cur = current_cli(text)
    cli = ask("每日決策要用哪支模型 CLI？claude＝claude -p；codex＝codex exec（唯讀沙箱）。"
              "兩者都只產出決策 JSON，不送單。",
              clis, cur if cur in clis else clis[0])
    if cli not in clis:
        return None
    model = ask(f"{cli} 用哪個模型？（推理強度固定 xhigh）",
                MODELS[cli], MODELS[cli][0])
    if model is None:
        return None
    return update_env(text, cli, model_value(model))


def main():
    ap = argparse.ArgumentParser(description="選決策層 CLI 與模型，寫進 agent.env")
    ap.add_argument("--if-missing", action="store_true",
                    help="agent.env 已設 ARK_DECISION_CLI 就不跳視窗（初次安裝用）")
    ap.add_argument("--env", default=ENV_PATH, help="agent.env 路徑（預設 ~/.ark-toolkit/agent.env）")
    args = ap.parse_args()

    text = open(args.env, encoding="utf-8").read() if os.path.exists(args.env) else ""
    if args.if_missing and current_cli(text):
        print(f"決策層：沿用 agent.env 的 {current_cli(text)}（要換：install.sh decision）")
        return 0
    if not available_clis():
        print("⚠️ 找不到 claude 或 codex CLI，決策層先用預設 claude；裝好後執行 install.sh decision")
        return 0
    new = run(text)
    if new is None:
        print("未變更（已取消）；未設定時決策層預設 claude／fable")
        return 0
    os.makedirs(os.path.dirname(args.env), exist_ok=True)
    with open(args.env, "w", encoding="utf-8") as fh:
        fh.write(new)
    picked = [l for l in new.splitlines() if l.startswith("ARK_DECISION_")]
    print(f"✅ 已寫入 {args.env}：{'、'.join(picked)}")
    print("   已裝排程的話，重跑 install.sh schedule 讓排程跟上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
