---
name: runway-testflight
description: 自作の iOS / iPadOS アプリを kajisho5/runway 経由で TestFlight に配布できるようにする手順。新しいアプリのリポを runway の配布先に足す、Secrets を配る、testflight ワークフローを置く、Apple 側の登録を案内する、初回の送信と「TestFlight に出てこない」の調査まで。「TestFlight に上げて」「runway に追加して」「TestFlight に出ない」と言われたときに使う。
---

# runway で TestFlight に配布する

Mac なしで GitHub Actions からビルド・署名・TestFlight 送信を行う。共通ワークフローは `kajisho5/runway/.github/workflows/apple-testflight.yml`、Secrets は runway に登録済みで `sync-secrets` が各アプリのリポへ配る。詳しい仕組みは runway の README。

## 守ること

- **Secrets の値は絶対に表示・コミットしない。** ログやチャットに出してよいのは Secret の名前と件数だけ。スクリプトもメールアドレスや鍵を出力しない。
- 利用者にトークンや鍵をチャットに貼らせない。
- Apple 側（developer.apple.com / App Store Connect）の画面操作は利用者にしかできない。URL と入力値を具体的に渡す。
- runway の main への変更は全アプリに即反映される。runway 側を直したら、何を変えたか（コミット）を利用者に伝える。

## 手順

### 1. 情報をそろえる

| 項目 | 例 | 決め方 |
|---|---|---|
| アプリのリポ | `kajisho5/iOS_QR_reader` | 利用者に確認 |
| Bundle ID | `com.seventhwell.seijitsuqr` | 利用者の組織 ID（例 `seventhwell`）＋アプリ名。仮の `com.example` が残っていれば全部差し替える（project.yml / 定数 / .storekit / README） |
| ビルド方法 | `.xcodeproj` / `.xcworkspace` / XcodeGen | リポを見て判断。XcodeGen なら `project.yml` があり `.xcodeproj` は .gitignore 済み |
| スキーム | `SeijitsuQR` | project.yml の `schemes:` か .xcodeproj |
| 必要な Capability | Hotspot Configuration など | `*.entitlements` / project.yml の `entitlements:` を見る |

アプリ側の前提（無ければ足す）:
- Info.plist に `ITSAppUsesNonExemptEncryption = NO`（輸出コンプライアンスの質問を出さない。暗号化を独自実装していない場合）
- バージョン（`CFBundleShortVersionString`）はプロジェクトで管理。ビルド番号は runway が TestFlight 上の最大値 +1 を付ける

### 2. runway の配布先に足す（Secrets の配布）

runway のリポを push 権限で用意し、`repos.txt` の末尾にアプリのリポを 1 行足して main に push する。push をきっかけに `sync-secrets` が走る。

```sh
echo "kajisho5/<app-repo>" >> repos.txt
git commit -am "Distribute secrets to kajisho5/<app-repo>" && git push origin main
```

確認: runway の Actions → sync-secrets の最新実行のログで、`== kajisho5/<app-repo>` の後に `::error::` が無いこと。配られるのは `ASC_KEY_ID` `ASC_ISSUER_ID` `ASC_KEY_P8_BASE64` `APPLE_TEAM_ID` `MATCH_GIT_TOKEN`（＋登録があれば `BETA_CONTACT_*`）。

### 3. アプリ側にワークフローを置く

`.github/workflows/testflight.yml`（runway の `templates/testflight.yml` が元）:

```yaml
name: testflight

on:
  workflow_dispatch:
  push:
    tags: ["v*"]

permissions:
  contents: read

jobs:
  ios:
    uses: kajisho5/runway/.github/workflows/apple-testflight.yml@main
    with:
      bundle_id: com.example.myapp        # 1. の Bundle ID
      project: MyApp.xcodeproj            # または workspace:
      scheme: MyApp
      # XcodeGen のときだけ（.xcodeproj をコミットしていない）
      pre_build: brew install xcodegen && xcodegen generate
      runner: macos-26
      xcode: "26"
    secrets: inherit
```

状態確認用に `scripts/asc_status.py` と `scripts/asc_internal_group.py` を `tools/` にコピーし、次のワークフローも置く（手動実行で使う）:

```yaml
# .github/workflows/testflight-status.yml
name: testflight-status

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  status:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v6
      - run: pip install --quiet pyjwt cryptography
      - name: Ensure the internal TestFlight group
        run: python3 tools/asc_internal_group.py
        env:
          BUNDLE_ID: com.example.myapp
          ASC_KEY_ID: ${{ secrets.ASC_KEY_ID }}
          ASC_ISSUER_ID: ${{ secrets.ASC_ISSUER_ID }}
          ASC_KEY_P8_BASE64: ${{ secrets.ASC_KEY_P8_BASE64 }}
      - name: Show build state
        run: python3 tools/asc_status.py
        env:
          BUNDLE_ID: com.example.myapp
          ASC_KEY_ID: ${{ secrets.ASC_KEY_ID }}
          ASC_ISSUER_ID: ${{ secrets.ASC_ISSUER_ID }}
          ASC_KEY_P8_BASE64: ${{ secrets.ASC_KEY_P8_BASE64 }}
```

README に「TestFlight への配布」節（初回の手作業と実行方法）を足す。

### 4. Apple 側の登録を案内する（利用者の作業）

上から順に。ログインした状態で開く。

1. **Bundle ID**: https://developer.apple.com/account/resources/identifiers/add/bundleId
   App IDs → App → Explicit に Bundle ID を入れ、1. で調べた Capability（例 Hotspot）にチェック → Register。
   Capability を付け忘れると署名（Archive）で失敗する。
2. **アプリ作成**: https://appstoreconnect.apple.com/apps → ＋ → 新規 App（iOS、Bundle ID は 1.、SKU は任意）
3. **App 内課金**（あれば）: アプリ → 収益化 → App 内課金 → ＋。製品 ID はアプリのコードの定数と一致させる
4. **内部テスト**: アプリ → TestFlight → 内部テスト → ＋、自分を追加して自動配信をオン
   （保存されていないことがある。6. で API から作り直せる）

### 5. 初回の送信

- `workflow_dispatch`（Run workflow）は **既定ブランチ（main）にあるワークフローしか動かせない**。ワークフローを足した PR を先に main へマージしてから、Actions → testflight → Run workflow（API なら `ref: main`）。
- タグ（`v*`）の push でも動くが、Claude のセッションからはタグを push できないことがある。その場合はマージして手動実行にする。
- 所要: ビルド〜送信で 3〜5 分、その後 App Store Connect の処理待ち（runway は最大 30 分待つ）。`Upload to TestFlight` が長いのは多くの場合この処理待ち。
- 成功時のログ: `state: COMPLETE` → `TestFlight に届いた`。

### 6. TestFlight に出てこないとき

`testflight-status` を手動実行してログを見る。内部グループが無ければ作り（全ビルド自動配信）、アカウント所有者・管理者をテスターに追加し、未承認なら招待を送る。

| ログの表示 | 意味 | 対処 |
|---|---|---|
| `processingState: PROCESSING` | Apple が処理中 | 数分待つ |
| `processingState: INVALID` | Apple がビルドを拒否 | Apple からのメール（ITMS-xxxxx）を利用者に確認してもらい、原因を直して再送 |
| `internalBuildState: READY_FOR_BETA_TESTING` かつグループ 0 件 | 配信先が無い | `asc_internal_group.py` がグループを作る。再実行後 `IN_BETA_TESTING` になる |
| テスター `state=INVITED` | 招待を承認していない | 利用者に App Store Connect のメールアドレスに届いた招待メールを承認してもらう。TestFlight アプリの Apple ID が App Store Connect と同じか確認 |
| `state=ACCEPTED` / `INSTALLED` なのに出ない | 端末側 | TestFlight アプリを開き直す、Apple ID を確認 |

## つまずきやすい点（実績）

| 症状 | 原因 | 対処 |
|---|---|---|
| `Import signing certificates` で `SecKeychainItemImport: MAC verification failed during PKCS12 import (wrong password?)` | パスワード違いではない。OpenSSL 3 既定の .p12（AES-256 + SHA-256 MAC）を macOS の `security import` が読めない | runway 3bd7902 で修正済み（`-keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1`）。再発したら `openssl pkcs12 -export` の引数を確認 |
| `App Store Connect に Bundle ID … のアプリが無い` | 4-2 が未実施か Bundle ID の不一致 | アプリの Bundle ID と App Store Connect を照合 |
| `Secret … が未登録` | sync-secrets 未実行、または repos.txt に無い | 2. をやり直す |
| Archive の署名エラー | Capability が Bundle ID に付いていない | 4-1 で Capability をオン |
| `workflow_dispatch` が 404 | ワークフローが main に無い | PR をマージしてから実行 |
| アプリの CI で `Unable to find a device matching the provided destination specifier` | ランナーイメージ更新でシミュレータ名・OS が変わった | シミュレータを決め打ちせず、入っているものから選ぶ（`xcrun simctl list devices available -j` を見て選ぶスクリプト、無ければ `xcodebuild -downloadPlatform iOS`） |
| `Node.js 20 is deprecated` の警告 | actions のバージョンが古い | `actions/checkout@v6`、`actions/upload-artifact@v6` に上げる |

## 終わったら利用者に伝えること

- 届いたビルド（バージョンとビルド番号）と、TestFlight アプリで確認してほしい点
- runway に入れた変更（repos.txt の追加、修正があればコミット）
- 公開前に残っている作業（プライバシーポリシー・利用規約の URL、App のプライバシー、スクリーンショット、審査用の情報など）
