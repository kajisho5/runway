#!/usr/bin/env bash
# repos.txt に書いた全リポへ、secrets.env の値を GitHub Actions Secrets として一括投入する。
# 使い方（runway のルートで）: bash scripts/sync-secrets.sh [secrets.env] [repos.txt]
# 必要: GitHub CLI (gh) にログイン済み。Windows は Git Bash で動く。
set -euo pipefail

ENV_FILE="${1:-secrets.env}"
REPOS_FILE="${2:-repos.txt}"
KEYS=(ASC_KEY_ID ASC_ISSUER_ID ASC_KEY_P8_BASE64 APPLE_TEAM_ID MATCH_PASSWORD MATCH_GIT_TOKEN)

die() { echo "エラー: $*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "gh が無い（https://cli.github.com/）"
gh auth status >/dev/null 2>&1 || die "gh 未ログイン。gh auth login を先に実行"
[ -f "$ENV_FILE" ] || die "$ENV_FILE が無い（secrets.env.example をコピーして作る）"
[ -f "$REPOS_FILE" ] || die "$REPOS_FILE が無い"

# secrets.env を source せずに安全に読む（KEY=VALUE、# はコメント）
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|'#'*) continue ;; esac
  [[ "$line" == *=* ]] || die "$ENV_FILE の書式不正: $line"
  key="${line%%=*}"; key="${key// /}"
  val="${line#*=}"
  if [[ "$val" == \"*\" ]]; then val="${val:1:${#val}-2}"; fi
  if [[ "$val" == \'*\' ]]; then val="${val:1:${#val}-2}"; fi
  [[ "$key" =~ ^[A-Z0-9_]+$ ]] || die "キー名不正: $key"
  printf -v "SV_$key" '%s' "$val"
done < "$ENV_FILE"

# .p8 のパスが書かれていれば base64 化する
if [ -z "${SV_ASC_KEY_P8_BASE64:-}" ] && [ -n "${SV_ASC_KEY_P8_PATH:-}" ]; then
  [ -f "$SV_ASC_KEY_P8_PATH" ] || die ".p8 が見つからない: $SV_ASC_KEY_P8_PATH"
  SV_ASC_KEY_P8_BASE64="$(base64 < "$SV_ASC_KEY_P8_PATH" | tr -d '\r\n')"
fi

for k in "${KEYS[@]}"; do
  name="SV_$k"
  [ -n "${!name:-}" ] || die "$k が $ENV_FILE に無い"
done

repos=()
while IFS= read -r r || [ -n "$r" ]; do
  r="${r%$'\r'}"; r="${r%%#*}"; r="${r//[[:space:]]/}"
  [ -n "$r" ] && repos+=("$r")
done < "$REPOS_FILE"
[ "${#repos[@]}" -gt 0 ] || die "$REPOS_FILE に対象リポが無い"

for repo in "${repos[@]}"; do
  echo "== $repo"
  for k in "${KEYS[@]}"; do
    name="SV_$k"
    printf '%s' "${!name}" | gh secret set "$k" --repo "$repo"
  done
done
echo "完了: ${#repos[@]} リポ × ${#KEYS[@]} 件"
