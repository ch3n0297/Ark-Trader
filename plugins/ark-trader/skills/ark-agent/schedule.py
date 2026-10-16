# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = []
# ///
"""launchd 排程的產生、載入與移除。由 `install.sh schedule`／`unschedule` 呼叫。

    schedule.py install --uv PATH [--live] [--jobs decide,settle] [--dry-run]
    schedule.py uninstall

`launchd/*.plist.in` 是範本，`@NAME@` 佔位符依**這台機器**代入：uv 的絕對路徑、
plugin 連結（`~/.ark-toolkit/plugin`，plugin 更新後重跑 install.sh 就跟上）、`HOME`、
PATH（含 uv 與決策層 CLI——claude 或 codex——所在目錄；launchd 不讀 shell 設定檔）。

送單後端只由 `--live` 決定，預設 simulation：`~/.ark-toolkit/agent.env` 裡的
`ARK_BACKEND` 不算數，切真錢一定要在終端機明確打出 `--live`。agent.env 的其他設定
（`ARK_RULES`、`ARK_DECISION_CLI`、`ARK_DECISION_MODEL`…）以 bash `source` 的語意
解讀後寫進 plist，與手動 `daily.py trade` 用的是同一份設定。

`~/Library/LaunchAgents/` 若有其他名稱的 ark-agent 排程（例如舊版手動複製的 plist）
就拒絕安裝：兩套排程同時跑，同一天會做兩次決策。
"""
import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "launchd")

LABEL_PREFIX = "local.ark-trader."
JOBS = ("decide", "settle", "review", "check")
SIMULATION, LIVE = "simulation", "live"

# 測試以替身取代，避免真的動到使用者的 launchd
LAUNCHCTL = os.environ.get("ARK_LAUNCHCTL", "/bin/launchctl")

# agent.env 不能覆寫的鍵：後端只由 --live 決定；PATH／HOME 由本檔依機器算；
# 其餘是 bash 自己會變動的變數，不是設定
RESERVED_ENV = frozenset({"ARK_BACKEND", "PATH", "HOME", "_", "PWD", "OLDPWD", "SHLVL"})
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 決策層可用的 CLI（與 daily.DECISION_CLIS 相同）；複盤固定走 claude
DECISION_CLIS = ("claude", "codex")

SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

PLACEHOLDER = re.compile(r"@([A-Z_]+)@")


# ---------------------------------------------------------------- 純邏輯

def label(job):
    return LABEL_PREFIX + job


def data_dir(home):
    return os.path.join(home, ".ark-toolkit")


def plist_path(home, job):
    return os.path.join(home, "Library", "LaunchAgents", label(job) + ".plist")


def build_path(home, uv, *clis):
    """launchd 的 PATH：uv 與模型 CLI（claude／codex）所在目錄優先，其後系統目錄，去重保序。
    codex 若是 npm 裝的 node 腳本，node 通常在同一個目錄，一併帶到。"""
    dirs = [os.path.dirname(uv)]
    dirs += [os.path.dirname(c) for c in clis if c]
    dirs += list(SYSTEM_PATH) + [os.path.join(home, ".local", "bin")]
    out = []
    for d in dirs:
        if d and d not in out:
            out.append(d)
    return ":".join(out)


def parse_env0(raw):
    """`env -0` 的輸出 → dict"""
    out = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        k, v = item.split(b"=", 1)
        out[k.decode()] = v.decode()
    return out


def env_overrides(before, after):
    """source agent.env 前後的環境差異＝個人化設定；保留鍵與不合法的鍵剔除"""
    return {k: v for k, v in after.items()
            if before.get(k) != v and k not in RESERVED_ENV and ENV_KEY.match(k)}


def decision_cli(extra_env):
    """agent.env 的 ARK_DECISION_CLI（未設或空白＝claude）；不認得的值報錯，不裝半套排程"""
    cli = (extra_env or {}).get("ARK_DECISION_CLI", "").strip() or "claude"
    if cli not in DECISION_CLIS:
        raise ValueError(f"ARK_DECISION_CLI 只能是 {'、'.join(DECISION_CLIS)}，現在是 {cli!r}")
    return cli


def needed_clis(cli, jobs):
    """這組排程要找哪些模型 CLI：decide／check 用決策層的 CLI，review 固定用 claude"""
    out = []
    if {"decide", "check"} & set(jobs):
        out.append(cli)
    if "review" in jobs:
        out.append("claude")
    return list(dict.fromkeys(out))


def values_for(job, home, uv, plugin_dir, backend, path):
    if backend not in (SIMULATION, LIVE):
        raise ValueError(f"未知的後端：{backend}")
    return {
        "LABEL": label(job),
        "UV": uv,
        "DAILY": os.path.join(plugin_dir, "skills", "ark-agent", "daily.py"),
        "WORKDIR": data_dir(home),
        "PATH": path,
        "HOME": home,
        "BACKEND": backend,
        "LOG_DIR": os.path.join(data_dir(home), "agent"),
    }


def render(template, values, extra_env=None):
    """代入佔位符並驗證結果是合法 plist。範本裡有沒給值的佔位符就報錯，不留半成品"""
    extra = "".join(f"    <key>{escape(k)}</key>\n    <string>{escape(v)}</string>\n"
                    for k, v in sorted((extra_env or {}).items()))

    def sub(m):
        name = m.group(1)
        if name == "EXTRA_ENV":
            return extra
        if name not in values:
            raise KeyError(f"範本佔位符沒有對應的值：@{name}@")
        return escape(values[name])

    text = PLACEHOLDER.sub(sub, template)
    plistlib.loads(text.encode())
    return text


def legacy_plists(agents_dir):
    """其他名稱的 ark-agent 排程 → [(label, path)]"""
    found = []
    if not os.path.isdir(agents_dir):
        return found
    for name in sorted(os.listdir(agents_dir)):
        if not name.endswith(".plist") or "ark-agent" not in name or name.startswith(LABEL_PREFIX):
            continue
        path = os.path.join(agents_dir, name)
        try:
            with open(path, "rb") as f:
                lbl = plistlib.load(f).get("Label") or name[:-len(".plist")]
        except Exception:                                       # noqa: BLE001
            lbl = name[:-len(".plist")]
        found.append((lbl, path))
    return found


# ---------------------------------------------------------------- 副作用

def run_launchctl(*args):
    p = subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, check=False)
    return p.returncode, (p.stdout + p.stderr).strip()


def load_agent_env(home):
    """以 bash source 的語意讀 agent.env（與 daily.py trade 手動一場相同），回傳差異"""
    path = os.path.join(data_dir(home), "agent.env")
    if not os.path.isfile(path):
        return {}
    base = ["/usr/bin/env", "-i", f"HOME={home}", "PATH=/usr/bin:/bin", "/bin/bash", "-c"]
    before = subprocess.run([*base, "env -0"], capture_output=True, check=True).stdout
    after = subprocess.run([*base, 'set -a; source "$1" >/dev/null; env -0', "_", path],
                           capture_output=True, check=True).stdout
    return env_overrides(parse_env0(before), parse_env0(after))


def find_cli(name, home):
    found = shutil.which(name)
    if found:
        return found
    extra = [os.path.join(home, ".claude", "local", "claude")] if name == "claude" else []
    for c in (os.path.join(home, ".local", "bin", name), *extra,
              f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if os.access(c, os.X_OK):
            return c
    return None


def install(jobs, backend, home, uv, plugin_dir, *, uid, launchctl=run_launchctl,
            extra_env=None, clis=(), dry_run=False, out=print):
    legacy = legacy_plists(os.path.join(home, "Library", "LaunchAgents"))
    if legacy:
        out("🛑 發現其他名稱的 ark-agent 排程，兩套同時跑會在同一天做兩次決策。先移除再重跑：")
        for lbl, path in legacy:
            out(f"    launchctl bootout gui/{uid}/{lbl}; rm '{path}'")
        return 2
    daily = os.path.join(plugin_dir, "skills", "ark-agent", "daily.py")
    if not os.path.isfile(daily):
        out(f"🛑 找不到 {daily}（plugin 連結失效？重跑 install.sh）")
        return 1

    path = build_path(home, uv, *clis)
    for job in jobs:
        with open(os.path.join(TEMPLATES, f"{job}.plist.in"), encoding="utf-8") as f:
            text = render(f.read(), values_for(job, home, uv, plugin_dir, backend, path), extra_env)
        if dry_run:
            out(f"# ---- {plist_path(home, job)}")
            out(text)
            continue
        dest = plist_path(home, job)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.makedirs(os.path.join(data_dir(home), "agent"), exist_ok=True)
        launchctl("bootout", f"gui/{uid}/{label(job)}")      # 沒載入過會失敗，正常
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        launchctl("enable", f"gui/{uid}/{label(job)}")        # 先前 disable 過的要解開才載得進去
        rc, msg = launchctl("bootstrap", f"gui/{uid}", dest)
        if rc != 0:
            out(f"🛑 載入 {label(job)} 失敗：{msg}")
            return 1
        out(f"  ✅ {label(job)}")
    return 0


def uninstall(home, *, uid, launchctl=run_launchctl, out=print):
    for job in JOBS:
        launchctl("bootout", f"gui/{uid}/{label(job)}")
        dest = plist_path(home, job)
        if os.path.exists(dest):
            os.remove(dest)
            out(f"  🗑  {label(job)}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="ark-agent launchd 排程（由 install.sh 呼叫）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="產生並載入排程")
    p.add_argument("--uv", default=shutil.which("uv"), help="uv 的絕對路徑")
    p.add_argument("--plugin-dir", default=os.path.expanduser("~/.ark-toolkit/plugin"))
    p.add_argument("--live", action="store_true", help="送真錢委託（預設 simulation）")
    p.add_argument("--yes", action="store_true", help="--live 時略過確認提問")
    p.add_argument("--jobs", default=",".join(JOBS), help=f"逗號分隔，預設 {','.join(JOBS)}")
    p.add_argument("--dry-run", action="store_true", help="只印出產生的 plist，不寫檔、不載入")
    sub.add_parser("uninstall", help="卸載並刪除排程")
    args = ap.parse_args(argv)

    home = os.path.expanduser("~")
    uid = os.getuid()
    if args.cmd == "uninstall":
        return uninstall(home, uid=uid)

    jobs = [j.strip() for j in args.jobs.split(",") if j.strip()]
    bad = [j for j in jobs if j not in JOBS]
    if bad or not jobs:
        ap.error(f"未知的排程：{','.join(bad) or '（空）'}；可用 {','.join(JOBS)}")
    if not args.uv or not os.path.isabs(args.uv):
        ap.error("需要 uv 的絕對路徑（--uv）")

    extra = load_agent_env(home)
    backend = LIVE if args.live else SIMULATION
    try:
        cli = decision_cli(extra)
    except ValueError as exc:
        print(f"🛑 {exc}（改 ~/.ark-toolkit/agent.env 後重跑）")
        return 1
    found = {name: find_cli(name, home) for name in needed_clis(cli, jobs)}

    print(f"排程：{', '.join(label(j) for j in jobs)}")
    print(f"送單後端：{backend}")
    print(f"uv：{args.uv}")
    print(f"plugin：{args.plugin_dir}")
    print(f"決策層 CLI：{cli}")
    if extra:
        print(f"agent.env 設定：{', '.join(sorted(extra))}")
    for name, where in found.items():
        if not where:
            print(f"⚠️ 找不到 {name} CLI：用到它的排程（決策層 {cli}、複盤層 claude）會失敗。"
                  "裝好後重跑 schedule。")
    if not args.live:
        print("（真錢委託需要 --live；agent.env 的 ARK_BACKEND 不影響排程）")
    elif not args.dry_run and not args.yes:
        print("⚠️ live：排程將在無人值守下自動送出真錢委託。切 live 前應先跑過 ark-start 驗證。")
        if not sys.stdin.isatty():
            print("🛑 非互動環境請加 --yes 確認")
            return 1
        if input("確定請輸入 live：").strip() != LIVE:
            print("已取消")
            return 1

    return install(jobs, backend, home, args.uv, args.plugin_dir, uid=uid,
                   extra_env=extra, clis=[c for c in found.values() if c],
                   dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
