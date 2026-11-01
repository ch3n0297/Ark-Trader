#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""以 macOS 原生視窗選 claude -p 的計費方式（訂閱或 API Key），寫進 ~/.ark-toolkit/agent.env。

install.sh 初次安裝時以 `--if-missing` 呼叫（agent.env 已有 ARK_ANTHROPIC_KEYCHAIN 這行
就不打擾，空值＝選了訂閱）；`install.sh apikey` 隨時重選。

API Key 只存在 Keychain（服務名 SERVICE），由使用者在終端機自己輸入，本檔不經手；
寫進 agent.env 的只有服務名。寫入前用乾淨環境跑一次便宜模型驗證，失敗就不改檔——
Key 無效時排程會整天不跑，要在安裝時就擋下來。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from choose_cli import ask_dialog

SERVICE = "ark-agent-anthropic"
SUBSCRIPTION = "訂閱（claude CLI 登入）"
API_KEY = "API Key（存在 Keychain）"
REUSE = "沿用 Keychain 裡的 Key"
REPLACE = "重新輸入 Key"
ENV_PATH = os.path.expanduser("~/.ark-toolkit/agent.env")
_KEY = re.compile(r"^\s*(?:export\s+)?ARK_ANTHROPIC_KEYCHAIN\s*=\s*(.*)$")


def current_auth(text):
    """agent.env 的 ARK_ANTHROPIC_KEYCHAIN：沒有這行回 None（還沒選過），空值回 ""（訂閱）。"""
    for line in text.splitlines():
        m = _KEY.match(line)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def update_env(text, service):
    kept = [l for l in text.splitlines() if not _KEY.match(l)]
    return "\n".join(kept + [f"ARK_ANTHROPIC_KEYCHAIN={service}"]) + "\n"


def verify_env(key, home=None, path=None):
    """只帶 HOME／PATH／Key：呼叫端（例如在 Claude Code 裡跑）的 ANTHROPIC_BASE_URL 等變數
    會把請求導去別處，回出假的「Credit balance is too low」。"""
    return {"HOME": home or os.path.expanduser("~"),
            "PATH": path or os.environ.get("PATH", "/usr/bin:/bin"),
            "ANTHROPIC_API_KEY": key}


def verify_command():
    """與 daily.model_command 同樣隔離（不讀個人設定），不給工具，用最便宜的模型。"""
    return ["claude", "-p", "只回覆兩個字母 OK", "--model", "haiku",
            "--setting-sources", "project", "--strict-mcp-config",
            "--disable-slash-commands", "--tools", "",
            "--output-format", "json"]


def parse_verify(returncode, stdout):
    """→ (是否成功, 說明)。"""
    try:
        r = json.loads(stdout)
    except ValueError:
        return False, stdout.strip()[:200] or f"離開碼 {returncode}"
    if returncode != 0 or r.get("is_error"):
        return False, str(r.get("result") or f"離開碼 {returncode}")[:200]
    return True, f"回覆 {r.get('result')!r}，花費 ${r.get('total_cost_usd', 0):.4f}"


def has_item():
    return subprocess.run(["security", "find-generic-password", "-s", SERVICE],
                          capture_output=True, check=False).returncode == 0


def store_key():
    """在終端機由使用者輸入 Key（-w 不帶值＝提示輸入，不留在 shell 歷史）。"""
    if not sys.stdin.isatty():
        print("⚠️ 非互動終端機，無法輸入 Key；請在終端機執行 install.sh apikey")
        return False
    print("請貼上 sk-ant-api03- 開頭的 API Key（Console → API Keys 建立；輸入時不會顯示）")
    return subprocess.run(["security", "add-generic-password", "-U", "-a",
                           os.environ.get("USER", ""), "-s", SERVICE, "-w"],
                          check=False).returncode == 0


def verify():
    done = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-w"],
                          capture_output=True, text=True, check=False)
    key = done.stdout.strip()
    if done.returncode != 0 or not key:
        return False, "Keychain 讀不到 Key"
    proc = subprocess.run(verify_command(), env=verify_env(key), capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"),
                          check=False, timeout=180)
    return parse_verify(proc.returncode, proc.stdout or proc.stderr)


def run(text, ask=ask_dialog, which=shutil.which, has_item=has_item,
        store_key=store_key, verify=verify, if_missing=False):
    """→ (新的 agent.env 內容或 None, 說明)。None＝不改檔（選過、沒裝 claude、取消、驗證失敗）。"""
    if if_missing and current_auth(text) is not None:
        return None, "已選過"
    if not which("claude"):
        return None, "找不到 claude CLI"
    cur = current_auth(text)
    pick = ask("claude -p（每日決策與週複盤）用哪種方式計費？"
               "API Key 會吃 Console 的額度，不受 CLI 登入過期影響。",
               [SUBSCRIPTION, API_KEY], API_KEY if cur else SUBSCRIPTION)
    if pick == SUBSCRIPTION:
        return update_env(text, ""), "改用訂閱"
    if pick != API_KEY:
        return None, "已取消"
    if has_item():
        again = ask("Keychain 已經有 Key，要沿用還是重新輸入？", [REUSE, REPLACE], REUSE)
        if again is None:
            return None, "已取消"
        if again == REPLACE and not store_key():
            return None, "Key 未存入"
    elif not store_key():
        return None, "Key 未存入"
    ok, msg = verify()
    if not ok:
        return None, f"驗證失敗：{msg}"
    return update_env(text, SERVICE), f"驗證通過（{msg}）"


def main():
    ap = argparse.ArgumentParser(description="選 claude -p 的計費方式，寫進 agent.env")
    ap.add_argument("--if-missing", action="store_true",
                    help="agent.env 已選過就不跳視窗（初次安裝用）")
    ap.add_argument("--env", default=ENV_PATH, help="agent.env 路徑（預設 ~/.ark-toolkit/agent.env）")
    args = ap.parse_args()

    text = open(args.env, encoding="utf-8").read() if os.path.exists(args.env) else ""
    new, msg = run(text, if_missing=args.if_missing)
    if new is None:
        if msg == "已選過":
            mode = "API Key" if current_auth(text) else "訂閱"
            print(f"claude 計費：沿用 agent.env 的設定（{mode}；要換：install.sh apikey）")
        else:
            print(f"未變更（{msg}）；未設定時 claude 用訂閱")
        return 1 if msg.startswith("驗證失敗") else 0
    os.makedirs(os.path.dirname(args.env), exist_ok=True)
    with open(args.env, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"✅ {msg}，已寫入 {args.env}：ARK_ANTHROPIC_KEYCHAIN={current_auth(new)}")
    print("   已裝排程的話，重跑 install.sh schedule 讓排程跟上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
