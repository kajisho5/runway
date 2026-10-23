# repos.txt に書いた全リポへ、secrets.env の値を GitHub Actions Secrets として一括投入する（PowerShell 版）。
# 使い方（runway のルートで）: pwsh -File scripts/sync-secrets.ps1 [-EnvFile secrets.env] [-ReposFile repos.txt]
# 必要: GitHub CLI (gh) にログイン済み。
param(
  [string]$EnvFile = "secrets.env",
  [string]$ReposFile = "repos.txt"
)
$ErrorActionPreference = "Stop"
$Keys = @("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_KEY_P8_BASE64", "APPLE_TEAM_ID", "MATCH_GIT_TOKEN")
# 任意（secrets.env にあれば配る）：TestFlight 外部テストの連絡先
$Optional = @("BETA_CONTACT_EMAIL", "BETA_CONTACT_FIRST", "BETA_CONTACT_LAST", "BETA_CONTACT_PHONE")

function Fail($msg) { Write-Error "エラー: $msg"; exit 1 }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Fail "gh が無い（https://cli.github.com/）" }
gh auth status *> $null
if ($LASTEXITCODE -ne 0) { Fail "gh 未ログイン。gh auth login を先に実行" }
if (-not (Test-Path $EnvFile)) { Fail "$EnvFile が無い（secrets.env.example をコピーして作る）" }
if (-not (Test-Path $ReposFile)) { Fail "$ReposFile が無い" }

$values = @{}
foreach ($raw in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
  $line = $raw.Trim()
  if ($line -eq "" -or $line.StartsWith("#")) { continue }
  $i = $line.IndexOf("=")
  if ($i -lt 1) { Fail "$EnvFile の書式不正: $line" }
  $k = $line.Substring(0, $i).Trim()
  $v = $line.Substring($i + 1)
  if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) {
    $v = $v.Substring(1, $v.Length - 2)
  }
  if ($k -notmatch '^[A-Z0-9_]+$') { Fail "キー名不正: $k" }
  $values[$k] = $v
}

if (-not $values["ASC_KEY_P8_BASE64"] -and $values["ASC_KEY_P8_PATH"]) {
  $p8 = $values["ASC_KEY_P8_PATH"]
  if (-not (Test-Path -LiteralPath $p8)) { Fail ".p8 が見つからない: $p8" }
  $values["ASC_KEY_P8_BASE64"] = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $p8)))
}

foreach ($k in $Keys) { if (-not $values[$k]) { Fail "$k が $EnvFile に無い" } }

$repos = @()
foreach ($raw in Get-Content -LiteralPath $ReposFile -Encoding UTF8) {
  $r = ($raw -replace '#.*$', '').Trim()
  if ($r) { $repos += $r }
}
if ($repos.Count -eq 0) { Fail "$ReposFile に対象リポが無い" }

foreach ($repo in $repos) {
  Write-Host "== $repo"
  foreach ($k in ($Keys + $Optional)) {
    if (-not $values[$k]) { continue }
    gh secret set $k --repo $repo --body $values[$k]
    if ($LASTEXITCODE -ne 0) { Fail "$repo の $k 設定に失敗" }
  }
}
Write-Host "完了: $($repos.Count) リポ × $($Keys.Count) 件"
