# runway

自作の Apple アプリ（iPhone / iPad / Apple Watch 同梱 / Apple TV / Vision Pro）を、**Mac なしで** GitHub Actions からビルドして TestFlight に送るための共通基盤。

方式は kajisho5/crosspoint で TestFlight 到達済みのものを汎用化したもの。

- **署名**：xcodebuild の自動署名 ＋ App Store Connect API キー。プロファイルは Apple 側で自動発行されるので、Watch や Widget などの同梱ターゲットも設定不要
- **証明書**：全アプリで 1 組を共有し、hanger に暗号化して置く。毎回新しい証明書が作られて Apple の上限に当たるのを防ぐ
- **送信**：App Store Connect API の Build Uploads。xcodebuild の upload は鍵を拒否した実績があるので使わない

| リポ | 役割 |
|---|---|
| `kajisho5/runway`（private） | 共通ワークフロー、証明書管理、Secrets 配布 |
| `kajisho5/hanger`（private） | 共有の署名用証明書（`signing/`）。apple-signing が書き込むので手で触らない |
| 各アプリのリポ | `templates/testflight.yml` をコピーして数行書き換えるだけ |

## 中身

```
.github/workflows/apple-testflight.yml  アプリ側から呼ぶ共通ワークフロー（ビルド → 署名 → TestFlight）
.github/workflows/apple-signing.yml     共有証明書の用意・確認（runway の Actions から手動実行）
.github/workflows/mac-notarize.yml      Mac 向けプラグイン/アプリのビルド → Developer ID 署名 → 公証 → dmg/zip
.github/workflows/sync-secrets.yml      runway の Secrets を repos.txt の全リポへコピー
.github/workflows/selfcheck.yml         runway 自身の整合性チェック
scripts/asc.py                          App Store Connect API ツール（apple-testflight.yml / mac-notarize.yml に埋め込み済み）
scripts/embed.py                        asc.py を編集したら実行して埋め込みを更新する
scripts/sync-secrets.sh / .ps1          sync-secrets を PC から行う場合（gh CLI が必要）
repos.txt                               Secrets の配布先
templates/testflight.yml                iPhone 等のアプリ側に置くワークフローの雛形
templates/mac-plugin.yml                Mac 向けプラグイン（VST3 / AU）側に置くワークフローの雛形
```

## Secrets

runway に登録して、sync-secrets で各アプリのリポへ配る。

| 名前 | 値 |
|---|---|
| `ASC_KEY_ID` | App Store Connect API キーの Key ID |
| `ASC_ISSUER_ID` | Issuer ID |
| `ASC_KEY_P8_BASE64` | `.p8` の中身（`-----BEGIN PRIVATE KEY-----` から `END` 行まで）。base64 でも可 |
| `APPLE_TEAM_ID` | Team ID |
| `MATCH_GIT_TOKEN` | hanger を読み書きできる Fine-grained PAT（Contents: Read and write）。名前は旧方式の名残 |
| `SYNC_TOKEN` | runway にだけ置く。配布用の Fine-grained PAT（All repositories / Secrets: Read and write） |
| `SIGNING_KEY_PASSWORD` | 任意。署名用秘密鍵のパスフレーズ。無ければ `.p8` から自動で導く |
| `BETA_CONTACT_EMAIL` / `BETA_CONTACT_FIRST` / `BETA_CONTACT_LAST` / `BETA_CONTACT_PHONE` | 任意。TestFlight 外部テスト（Beta App Review）の連絡先（メール・名・姓（ローマ字）・電話 +81…）。入れておくと各アプリにも配られ、外部テストの審査に自動で出せる |

API キーはアクセス権 **Admin**。自動署名がプロファイルを発行するのに必要。

## 初回だけ

1. runway の Settings → Actions → General → Access を「Accessible from repositories owned by the user」にする（済）
2. 上の Secrets を runway に登録する（`SYNC_TOKEN` 以外は済）
3. runway の Actions → **apple-signing** → Run workflow を `import-crosspoint` で実行する。crosspoint が持っている既存の証明書を hanger に移すので、新しい証明書は発行されない。`import-crosspoint` が失敗したら `create`（Apple 側に証明書が 2 枚増える）

`SYNC_TOKEN` は次の URL で、名前・権限が入った状態の作成画面が開く。Repository access の **All repositories** だけ選んで Generate token を押す。

https://github.com/settings/personal-access-tokens/new?name=runway-sync-secrets&description=runway+sync-secrets%3A+write+Actions+secrets+to+app+repos&expires_in=none&secrets=write

## 新しいアプリを足すとき

1. **Bundle ID を登録する**：developer.apple.com → Certificates, Identifiers & Profiles → Identifiers → ＋ → App IDs → App → Explicit。Watch や Widget を同梱するならその分も登録する
2. **アプリ記録を作る**：App Store Connect → アプリ → ＋ → 新規 App（Bundle ID は 1 のもの）
3. **TestFlight の内部グループを作る**：アプリ → TestFlight → 内部テスト → ＋ → 自分を追加し、自動配信をオン
4. **Secrets を配る**：runway の `repos.txt` にアプリのリポを 1 行足す（GitHub の画面で編集して保存すれば、sync-secrets が自動で走る）
5. **ワークフローを置く**：アプリのリポに `templates/testflight.yml` を `.github/workflows/testflight.yml` としてコピーし、`bundle_id` / `project`（または `workspace`）/ `scheme` を書き換える
6. **実行する**：アプリのリポの Actions → testflight → Run workflow。iPhone の GitHub アプリからも押せる。所要 15〜35 分

### 呼び出し側の前提

- アプリのバージョン（`CFBundleShortVersionString`）はプロジェクト側で管理する。ビルド番号は TestFlight 上の最大値 +1 を自動で付ける
- `ITSAppUsesNonExemptEncryption = NO` を Info.plist に入れておくと、TestFlight で輸出コンプライアンスの質問が出ない
- 対応プラットフォームは `ios` / `tvos` / `visionos`。macOS は未対応


## Mac 向けプラグイン・アプリ（Developer ID 署名 + 公証）

TestFlight ではなく、Apple の公証（notarization）を通した `.dmg` / `.zip` を作る。DAW 用の VST3 / AU プラグインや、App Store 外で配る Mac アプリ向け。

1. **初回だけ**：runway の Actions → **apple-signing** を `create-developer-id` で実行する。API で Developer ID 証明書を作れなかった場合は、実行結果の Summary に手動発行の手順が出る（hanger の CSR を developer.apple.com に上げて、できた .cer を hanger に置く）
2. **リポに置く**：プラグインのリポに `templates/mac-plugin.yml` を `.github/workflows/release-mac.yml` としてコピーし、`build`（ビルドコマンド）と `products`（できたバンドルのパス）を合わせる
3. **実行する**：Actions → release-mac → Run workflow、または `v1.0.0` のようなタグを push。公証済みの `.dmg` が Artifacts に出る

- **対象リポに Secrets が無くても**、runway の Actions → **mac-release** に対象リポ（public）を入れて実行すれば同じ署名・公証ができる。ただし runway は private なので macOS の無料枠を使う
- 公証は App Store Connect API のチームキーで行う（`notarytool --key`）。今の API キーをそのまま使う
- 2026/10/16 に実機ランナーで通し確認済み（ダミーの VST3 を署名 → 公証 Accepted → ステープル）
- **アプリ（.app）も対象**：同梱の拡張（.appex。Finder のクイックルック拡張など）と、`Contents/MacOS`・`Contents/Helpers` の本体以外の実行ファイル（同梱のコマンドなど）も署名し直す。これらはビルド時に埋め込まれた entitlements（拡張のサンドボックスなど）を残したまま署名する。本体の entitlements は `entitlements` 入力で渡す
- dmg に「アプリケーション」フォルダへのリンクを入れるときは `app_link: true`（ドラッグで入れる形の dmg になる）。実例は kajisho5/zippeek の `.github/workflows/release.yml`
- AAX（Pro Tools）は PACE の署名が別途必要なので対象外
- Windows 版（VST3）の署名は別の仕組みが必要（未対応）

## うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| `Secret … が未登録` | sync-secrets が未実行か、`repos.txt` にリポが無い |
| `App Store Connect に Bundle ID … のアプリが無い` | 「新しいアプリを足すとき」の 2 が未実施か、Bundle ID の不一致 |
| `GET /v1/users -> HTTP 403` の警告 | API キーが Admin ではない |
| `署名用証明書が無い` | apple-signing が未実行 |
| `署名用秘密鍵を復号できない` | `.p8` を変えた。apple-signing を `create` で実行し直す |
| archive で `maximum number of certificates` | Apple の証明書上限。apple-signing を `revoke-and-create` で実行する（crosspoint の ci/signing も使えなくなる点に注意） |
| エラー 90725 | Xcode 26 が選ばれていない。`xcode` 入力とランナーを確認 |
| `Xcode 26 が無い` | ランナーイメージが変わった。`runner: macos-26` などに変える |

## 費用

- public なアプリのリポなら、GitHub Actions の標準ランナーは無料
- private なリポは無料枠を消費し、macOS は Linux の 10 倍で減る（Free プランの 2,000 分なら、macOS で実質 200 分、ビルド 10 回前後）
- runway 側の apple-signing / sync-secrets は Linux ランナーで数十秒なので、ほぼ消費しない

## 確認済み / 未確認

- **確認済み**：ワークフロー構文（actionlint + shellcheck）。`asc.py` の各コマンドを模擬 API サーバーで確認（アプリ検索、ビルド番号、証明書の作成と失効、分割アップロードと MD5、処理待ち）。crosspoint 方式で暗号化された秘密鍵の復号（改行や CRLF の違いを含む 12 通り）
- **未確認**：本物の Mac ランナーでの通し実行。tvOS / visionOS / Watch 同梱の実アプリでの動作。中核の処理（自動署名、Build Uploads）は crosspoint で TestFlight 到達済み

## 更新ルール

各アプリは `@main` で呼ぶ（1 人開発なのでタグ管理を省く）。runway の main を直せば全アプリに即反映される。push 時に selfcheck が走る。
互換性が壊れる変更をするときだけ、事前にタグ（例: `v2`）を切って各アプリをそのタグに固定する。古い `v1` タグは旧方式（fastlane match）時代のもので、使っていない。

`scripts/asc.py` を編集したら `python3 scripts/embed.py` を実行してからコミットする。忘れると selfcheck が落ちる。
