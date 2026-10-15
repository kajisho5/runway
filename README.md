# runway

Apple 全プラットフォーム（iPhone / iPad / Apple Watch / Mac / Apple TV / Vision Pro）のアプリを、**Mac なしで** GitHub Actions からビルドして TestFlight に配布するための共通基盤。

| リポ | 公開範囲 | 役割 |
|---|---|---|
| `kajisho5/runway` | private | 共通ワークフロー、証明書作成ワークフロー、Secrets 配布スクリプト |
| `kajisho5/hanger` | private | 証明書・プロファイルの保管庫。fastlane match が暗号化して書き込むので手で触らない |
| 各アプリのリポ | 任意 | `templates/testflight.yml` をコピーして数行書き換えるだけ |

## 中身

```
.github/workflows/apple-testflight.yml  各アプリから呼ぶ共通ワークフロー（ビルド→署名→TestFlight）
.github/workflows/apple-certs.yml       証明書・プロファイルの作成/更新（runway の Actions タブから手動実行）
.github/workflows/sync-secrets.yml     runway の Secrets を repos.txt の全リポへコピー（ブラウザから実行）
scripts/sync-secrets.sh / .ps1          同じことを PC から行う場合のスクリプト
repos.txt                               Secrets の配布先リスト
secrets.env.example                     Secrets の雛形（secrets.env にコピーして使う）
templates/testflight.yml                アプリ側に置く呼び出しワークフローの雛形
```

## 初回セットアップ（1回だけ）

1. **runway の共有設定**
   runway の Settings → Actions → General → Access で「Accessible from repositories owned by the user」を選ぶ。これをしないと、他リポから private な runway のワークフローを呼べない。

2. **hanger を確認**
   private になっていることを確認する。中身は空でよい（match がブランチ `main` に書き込む）。

3. **App Store Connect API キーを作る**
   App Store Connect → ユーザとアクセス → 統合 → App Store Connect API → チームキーで生成する。
   - 証明書とプロファイルを作らせるので、アクセス権は Admin を選ぶ
   - `.p8` は1回しかダウンロードできない
   - Key ID と Issuer ID を控える

4. **GitHub の Fine-grained PAT を作る**
   GitHub → Settings → Developer settings → Fine-grained tokens で作成する。
   - Repository access：`hanger` のみ
   - Permissions：Contents を Read and write
   - 有効期限が切れたら作り直して手順 6 をやり直す

5. **Team ID を控える**
   developer.apple.com → Membership details に載っている。

6. **Secrets を配る（ブラウザだけで完結）**
   1. runway の Settings → Secrets and variables → Actions に次の7件を登録する。

      | 名前 | 値 |
      |---|---|
      | `ASC_KEY_ID` | API キーの Key ID |
      | `ASC_ISSUER_ID` | Issuer ID |
      | `ASC_KEY_P8_BASE64` | `.p8` の中身をそのまま貼る（`-----BEGIN PRIVATE KEY-----` から `END` 行まで）。base64 化した文字列でも可 |
      | `APPLE_TEAM_ID` | Team ID |
      | `MATCH_PASSWORD` | hanger 暗号化用のパスフレーズ（新しく決める。無くすと復号できない） |
      | `MATCH_GIT_TOKEN` | 手順 4 の PAT |
      | `SYNC_TOKEN` | 配布用の Fine-grained PAT（下記）。runway にだけ置き、配布はしない |

   2. `SYNC_TOKEN` を作る。GitHub → Settings → Developer settings → Fine-grained tokens で作成する。
      - Repository access：All repositories（新しいアプリリポにもそのまま配れる）
      - Permissions：Secrets を Read and write
   3. runway の Actions → **sync-secrets** → Run workflow で、`repos.txt` の全リポへ6件がコピーされる。`repos.txt` を編集して push したときも自動で実行される。

   PC から配りたい場合は、`scripts/sync-secrets.sh` / `.ps1` と `secrets.env.example` も使える（gh CLI が必要）。

## アプリを追加する手順

1. **Bundle ID を登録する**
   developer.apple.com → Certificates, Identifiers & Profiles → Identifiers で登録する。Watch や Extension を同梱するなら、その分も全部登録する。

2. **アプリを作成する**
   App Store Connect → アプリ → ＋ で新規アプリを作る（ブラウザだけでできる）。

3. **証明書とプロファイルを作る**
   runway の Actions → **apple-certs** → Run workflow を実行する。
   - platform：iPhone / Watch / Vision Pro なら `ios`、Apple TV なら `tvos`、Mac なら `macos`
   - app_identifiers：1 で登録した Bundle ID をカンマ区切りで全部
   - type：`appstore`

4. **Secrets の配布先に追加する**
   `repos.txt` にアプリのリポを足して push する（GitHub の画面で編集してもよい）。sync-secrets が自動で走り、Secrets が配られる。

5. **呼び出しワークフローを置く**
   アプリのリポに `templates/testflight.yml` を `.github/workflows/testflight.yml` としてコピーし、`project` / `scheme` / `signing_targets` を書き換える。

6. **実行する**
   Actions から手動実行するか、`v*` タグを push する。

### Xcode プロジェクト側の前提

- **ビルド番号**：Info.plist の `CFBundleVersion` が `$(CURRENT_PROJECT_VERSION)` になっていること（Xcode の新規プロジェクトの既定）。ワークフローが TestFlight 上の最新番号に +1 した値を全ターゲットに渡すので、Watch アプリも iOS アプリと同じ番号になる。
- **signing_targets**：`Target名=Bundle ID` を全ターゲット分書く。1行目はメインアプリにする。
- **署名設定**：CI 上で Release 構成の署名を手動署名（match のプロファイル）に書き換えてからビルドする。リポ内のプロジェクトファイルは変更されない（CI の作業コピーだけ）。

## プラットフォーム別メモ

| 対象 | platform | 補足 |
|---|---|---|
| iPhone / iPad | `ios` | 基本形 |
| Apple Watch | `ios` | iOS アプリに同梱する。`signing_targets` に Watch ターゲットを足し、`extra_sdks: watchOS` を指定する |
| Vision Pro | `visionos` | match に visionOS 区分が無いので、証明書・プロファイルは `ios` 扱い（fastlane の仕様） |
| Apple TV | `tvos` | |
| Mac | `macos` | Mac App Store 用。インストーラー証明書も自動で作られる |

## 費用

- GitHub Actions は、public リポの標準ランナーなら無料
- private リポは無料枠を消費し、macOS ランナーは Linux の10倍のペースで減る（Free プランの 2,000 分なら、macOS で実質 200 分）
- runway と hanger は private だが、`apple-certs` はたまにしか実行しないので消費は小さい。主に消費するのはアプリ側リポのビルド

## 未検証事項

- 実機の Actions ランナーでは、まだ1度も実行していない。確認済みなのはワークフロー構文（actionlint）と Fastfile の Ruby 構文、fastlane 2.241.1 のオプション名・取りうる値（ソースで確認）まで
- `macos` の .pkg 書き出しとインストーラー署名、`tvos`、`visionos` は、実アプリでの動作が未確認
- App Store Connect API キーで証明書を作るのに必要な最小ロールは未確認。Admin なら問題ない想定

## 更新ルール

各アプリは `@v1` で固定して呼ぶ。runway を直したら次のどちらかにする。
- 互換性がある修正：`v1` タグを付け直す（`git tag -f v1 && git push -f origin v1`）
- 互換性が壊れる修正：`v2` を切る
