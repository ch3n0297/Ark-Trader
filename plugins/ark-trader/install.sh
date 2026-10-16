#!/bin/bash
# ark-trader 安裝與排程（僅限 macOS）。流程寫死在這裡，每台機器跑出來的結果一致；
# 由使用者在終端機執行，不交給 coding agent 自行判斷步驟。
#
#   bash install.sh [--skip-setup]       安裝：macOS → uv → 預熱依賴 → 輔助使用權限 → ark-setup
#   bash install.sh schedule [--live] [--jobs decide,settle] [--dry-run]
#                                        依這台機器產生並載入 launchd 排程（預設 simulation）
#   bash install.sh unschedule           卸載並刪除排程
#
# plugin 更新後重跑一次 `bash install.sh`：~/.ark-toolkit/plugin 連結會指向新位置，
# 排程經由這個連結呼叫腳本，不必重新產生。
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="$HOME/.ark-toolkit"
LINK="$DATA/plugin"
UV=""

say()  { printf '%s\n' "$*"; }
step() { printf '\n▶ %s\n' "$*"; }
die()  { printf '🛑 %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

require_macos() {
  [ "$(uname -s)" = "Darwin" ] || die "ark-trader 僅支援 macOS（目前是 $(uname -s)）"
  [ "$(uname -m)" = "arm64" ] || say "⚠️ 非 Apple Silicon：方舟運算依賴 iOS App 相容層，可能無法執行"
  [ -f "$ROOT/skills/ark-setup/setup.py" ] || die "找不到 plugin 檔案：$ROOT 不是 ark-trader plugin 目錄"
}

find_uv() {
  for c in "$(command -v uv 2>/dev/null || true)" "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -n "$c" ] && [ -x "$c" ]; then printf '%s' "$c"; return 0; fi
  done
  return 1
}

ensure_uv() {
  if UV="$(find_uv)"; then
    say "✅ uv：$UV（$("$UV" --version)）"
    return 0
  fi
  say "未找到 uv，開始安裝…"
  if command -v brew >/dev/null 2>&1; then
    brew install uv || say "⚠️ brew 安裝失敗，改用官方安裝程式"
  fi
  if ! UV="$(find_uv)"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv 官方安裝程式失敗"
  fi
  UV="$(find_uv)" || die "uv 安裝後仍找不到（預期在 ~/.local/bin/uv）"
  say "✅ uv：$UV（$("$UV" --version)）"
}

link_plugin() {
  mkdir -p "$DATA/agent"
  if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    die "$LINK 已存在且不是連結，請先移走再重跑"
  fi
  ln -sfn "$ROOT" "$LINK"
  say "✅ plugin 連結：$LINK → $ROOT"
}

prewarm() {
  local f n=0
  for f in "$ROOT"/skills/*/*.py; do
    case "$(basename "$f")" in test_*) continue ;; esac
    grep -q '^# /// script' "$f" || continue
    say "  ${f#"$ROOT"/}"
    "$UV" sync --quiet --script "$f" || die "預熱失敗：$f"
    n=$((n + 1))
  done
  say "✅ 已預熱 $n 支腳本的依賴"
}

ax_trusted() {
  # $1=1 時順便叫出系統的授權對話框（只在互動終端機）
  "$UV" run --quiet --no-project --python 3.13 --with pyobjc-framework-applicationservices \
    python -c '
import sys, ApplicationServices as A
prompt = sys.argv[1] == "1"
ok = A.AXIsProcessTrustedWithOptions({A.kAXTrustedCheckOptionPrompt: prompt}) if prompt else A.AXIsProcessTrusted()
sys.exit(0 if ok else 1)' "${1:-0}"
}

ensure_ax() {
  local app="${TERM_PROGRAM:-你的終端機}" ans
  if ax_trusted 0; then
    say "✅ 輔助使用權限：已授權"
    return 0
  fi
  say "⚠️ 「$app」尚未取得輔助使用權限（ark-sync／ark-analyze 等操作 ARK 畫面時需要）。"
  say "   系統設定 > 隱私權與安全性 > 輔助使用 → 打開「$app」"
  if [ ! -t 0 ]; then
    say "   （非互動環境，先略過；授權後重跑本腳本即可確認）"
    return 0
  fi
  ax_trusted 1 || true
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true
  while :; do
    printf '   授權完成後按 Enter 重新檢查（輸入 s 略過）：'
    read -r ans || ans=s
    [ "$ans" = "s" ] && { say "   已略過"; return 0; }
    if ax_trusted 0; then say "✅ 輔助使用權限：已授權"; return 0; fi
    say "   仍未授權；部分終端機要完全結束再開才會生效。"
  done
}

cmd_install() {
  local skip_setup=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --skip-setup) skip_setup=1 ;;
      -h|--help) usage 0 ;;
      *) say "未知參數：$1"; usage 1 ;;
    esac
    shift
  done

  step "1/5 檢查系統"
  require_macos
  say "✅ macOS $(sw_vers -productVersion 2>/dev/null || true)（$(uname -m)）"

  step "2/5 uv"
  ensure_uv
  link_plugin

  step "3/5 預熱 Python 與套件（第一次較久）"
  prewarm

  step "4/5 輔助使用權限"
  ensure_ax

  step "5/5 帳戶設定（ark-setup）"
  if [ "$skip_setup" = 1 ]; then
    say "已略過（--skip-setup）；之後可執行：\"$UV\" run \"$ROOT/skills/ark-setup/setup.py\""
  else
    "$UV" run "$ROOT/skills/ark-setup/setup.py" || die "ark-setup 未完成；修正後重跑本腳本"
  fi

  step "完成"
  say "下一步："
  say "  • 在 Claude Code／Codex 說「執行 ark-start 驗證環境」（模擬單，不碰真錢）"
  say "  • 要每日自動決策：bash \"$ROOT/install.sh\" schedule   （預設 simulation）"
  case ":$PATH:" in
    *":$(dirname "$UV"):"*) ;;
    *) say "  • uv 裝在 $(dirname "$UV")，目前 shell 的 PATH 還沒有它：開新的終端機視窗即可" ;;
  esac
}

cmd_schedule() {
  require_macos
  ensure_uv
  link_plugin
  "$UV" run --quiet "$ROOT/skills/ark-agent/schedule.py" install --uv "$UV" --plugin-dir "$LINK" ${1+"$@"}
}

cmd_unschedule() {
  require_macos
  ensure_uv
  "$UV" run --quiet "$ROOT/skills/ark-agent/schedule.py" uninstall
}

case "${1:-install}" in
  install)    [ $# -gt 0 ] && shift; cmd_install ${1+"$@"} ;;
  schedule)   shift; cmd_schedule ${1+"$@"} ;;
  unschedule) shift; cmd_unschedule ;;
  -h|--help|help) usage 0 ;;
  --*)        cmd_install ${1+"$@"} ;;
  *)          say "未知指令：$1"; usage 1 ;;
esac
