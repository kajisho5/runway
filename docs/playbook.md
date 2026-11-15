# Apple アプリを Mac なしで作って出す手順（Claude 向けの引き継ぎ書）

> **Claude へ**：新しいセッションで Apple のアプリ（iPhone・iPad・Apple Watch・Mac）を作る・出すときは、まずこのファイルを全部読むこと。
> ここに書いたことは、TwinKeys（`kajisho5/ableton-twinkeys`）で実際にやって、うまくいった方法です（2026年11月）。
> 実物のコードは TwinKeys のリポジトリにあります。読むときは `add_repo` で `kajisho5/ableton-twinkeys` を読み取り用に足してください。
> 持ち主は **iPhone しか持っていません（Mac・Windows なし）**。手順は iPhone の Safari と GitHub アプリでできる形で書くこと。

## 0. 大事な決まり

- **個人の情報（名前・メールアドレス・電話番号）はファイルに書かない。** runway の Secrets に入っている（`BETA_CONTACT_EMAIL` など、`secrets.env.example` を参照）。足りないものは持ち主に聞く。作って入れない。
- 鍵（App Store Connect API キー、証明書）も Secrets にある。ファイル・ログ・コミットに出さない。
- **審査に出すボタン（Add for Review → Submit for Review）は持ち主が押す。** 自動で出そうとすると Claude の安全チェックで止められた（2回）。準備は全部自動でやって、最後のボタンだけ手順を渡す。
- 見本のデータに実在の名前を使わない（明らかに架空のものにする）。
- 画面があるものは、設計書と絵（SVG→PNG）を見せて、承認をもらってから作る。

## 1. 全体の流れ

| 順番 | やること | 誰が | どこで |
|---|---|---|---|
| 1 | Bundle ID と App Store Connect のアプリを作る | 持ち主（iPhone の Safari） | 下の 3-1 |
| 2 | アプリのリポジトリを runway の配布先に足す | Claude | runway の `repos.txt` → Actions「sync-secrets」 |
| 3 | TestFlight に送るワークフローを置く | Claude | `templates/testflight.yml` をコピー |
| 4 | TestFlight で試す | 持ち主 | iPhone の TestFlight アプリ |
| 5 | App Store の中身を自動で入れる | Claude | `templates/appstore/`（下の 4） |
| 6 | 画面写真 | Claude | CI のシミュレーターで撮る（下の 5） |
| 7 | ヘッダー動画・検索結果の画像（任意） | Claude | Python で SVG → rsvg-convert → ffmpeg（下の 6） |
| 8 | 手でしかできない項目（プライバシーの答えなど） | 持ち主 | 下の 4-3 |
| 9 | 審査に出す | 持ち主 | 下の 7 |

## 2. もう済んでいること（アカウント全体で1回だけ）

- App Store Connect API キー、Team ID、共有の署名用証明書（`kajisho5/hanger`）→ runway の Secrets。
- 有料アプリの契約・税・口座 → 有効（2026年10月〜）。
- EU の「トレーダー」の申告（DSA）→ 有効。TwinKeys Remote は **EU の27か国では配っていない**（トレーダーとして住所などを出さないため）。新しい有料アプリも同じにするかは持ち主に聞く。

## 3. 新しいアプリの始め方

### 3-1. Bundle ID とアプリの登録（持ち主が iPhone で）
1. <https://developer.apple.com/account/resources/identifiers/list> →「＋」→「App IDs」→「App」→ 説明と Bundle ID（例 `io.github.kajisho5.<名前>`）→「Register」。Apple Watch アプリを付けるなら `<Bundle ID>.watchkitapp` も同じように作る。
2. <https://appstoreconnect.apple.com/apps> →「＋」→「新規 App」→ プラットフォーム・名前・主な言語・Bundle ID・SKU →「作成」。
3. 出てきた Apple ID（数字）を Claude に教える。

### 3-2. runway につなぐ（Claude）
- runway の `repos.txt` にアプリのリポジトリを1行足して push → Actions「sync-secrets」を動かす（Secrets がアプリのリポジトリにコピーされる）。
- `templates/testflight.yml` をアプリのリポジトリの `.github/workflows/testflight.yml` にコピーして、`bundle_id`・`project`・`scheme`（Watch があれば `extra_sdks: watchOS`）を書き換える。
- TwinKeys では、プロジェクトは XcodeGen（`iOS/project.yml`）で作り、ビルド番号は runway が自動で上げる。
- iPhone しかないので、**作業ブランチの特定のファイルを変えて push すると動く**ようにすると便利（TwinKeys：`iOS/testflight-request.txt` に1行足すと TestFlight に送る）。

## 4. App Store の中身を自動で入れる

`templates/appstore/` に TwinKeys で使った実物を置いてある：

| ファイル | 中身 |
|---|---|
| `asc_appstore.py` | App Store Connect API で「審査に出す直前」まで入れる（バージョン、名前・説明・キーワード（日・英）、カテゴリ、年齢制限、著作権、価格、配る国（EU を外す）、審査メモ、画面写真、ビルド）。何回動かしても大丈夫 |
| `asc_beta.py` | TestFlight の外部テスト（公開リンク、テスト内容、ベータ審査） |
| `appstore-prepare.yml` | 上を GitHub Actions で動かすワークフロー |
| `listing.example.json` | 入れる中身（TwinKeys の実物。文面を新しいアプリのものに書き換える） |

**新しいアプリに使うとき**：`asc_appstore.py` の中の「TwinKeys Remote」と書いてある所（アプリ名、無料・有料の文面チェック、最後の表示）を書き換える。`grep -n "TwinKeys" asc_appstore.py` で全部見つかる。

### 4-1. 分かったこと（つまずいた所）
- 価格：`manualPrices` が 404（"no resource of type 'null'"）を返すのは「まだ価格がない」という意味。エラーではない。
- 配る国：最初は `appAvailabilityV2` を作る。もうあるときは `PATCH /v1/territoryAvailabilities/{id}`（`available: false`）で EU の国だけ止める。
- 「このバージョンの新機能」は、最初のバージョンでは送れない（2つ目から）。
- Apple Watch の画面写真は、決まった大きさ（例 416×496）で、透明（アルファ）を取りのぞいてから送る。
- 主な言語は English (U.S.)、日本語も入れる。

### 4-2. 審査メモ
4000 バイト以内。Mac など相手の機械がないと動かないアプリは、**アプリの中に「サンプルで試す」ボタンを付ける**（架空のデータで全部の画面を見られる）と審査が通りやすい。TwinKeys Remote はこれで通った。

### 4-3. API でできないので持ち主が手でやる
1. App のプライバシー：「Get Started」→「No, we do not collect data from this app」→「Save」→「Publish」。
2. 有料アプリの契約などが切れていないか：<https://appstoreconnect.apple.com/business>
3. EU の申告の質問が出たら「This is not a trader account」（EU で配らない場合）。
4. ヘッダー動画・検索結果の画像（下の 6）：API では送れない（2026年11月時点で見つからなかった）。

## 5. 画面写真

- CI（macOS ランナー）のシミュレーターで撮る。アプリに起動引数（TwinKeys：`-demo`、`-target qlab` など）で見本のデータを出す仕組みを作っておく。
- 横向きで撮った画像は、向きを直してから使う（ffmpeg `transpose=2`）。
- App Store に必要な大きさ：iPhone 6.9 インチ（必須）・6.3 インチ、iPad 13 インチ、Apple Watch。

## 6. ヘッダー動画・検索結果の画像（任意、iOS 27 以降の App Store に出る）

- 大きさ：ヘッダー 21:9（3840×1646）の画像か動画（5〜30 秒、音なしでくり返す）、検索結果 3:2（3840×2560）。値段・URL・©・ほかのストアのロゴは入れない。日本語・英語の両方。
- 作り方：Python（標準ライブラリだけ）で SVG を書く → `rsvg-convert` で PNG → `ffmpeg` で H.264・30 fps・無音の AAC 付きの mp4。
- 持ち主に気に入られた作り：「キーノート風」（黒い背景、短い大きな見出し、アプリの画面を大きく、1場面ずつ）。分かりにくいもの・地味なものは却下された。実物：TwinKeys の `design/appstore-creative/keynote.py`、設計 `docs/design-appstore-creative.md`。
- **アプリの今の見た目で描くこと**（古い見た目で作ってやり直しになった）。
- 入れ方（持ち主）：App Store Connect のバージョンのページ →「Header and Search Results」→ ヘッダーと検索結果にそれぞれ選ぶ → 言語を日本語に変えて、日本語版も入れる。

## 7. 審査に出す（持ち主）

1. `https://appstoreconnect.apple.com/apps/<Apple ID>` → 左の「iOS アプリ」の下の「〇〇 提出準備中」。
2. ビルドの欄が正しい番号か確かめる。
3. 右上の「審査用に追加」→「審査へ提出」。
4. 結果はふつう1日くらい。通ったら自動で公開（「手動でリリース」にしているときは「このバージョンをリリース」を押す）。

## 8. Mac アプリ・Windows アプリ

- Mac（App Store 外で配る）：runway の `mac-notarize.yml` で Developer ID の署名と公証。TwinKeys は `VERSION` ファイルを変えて main に入れると、ビルド・公証・GitHub のリリースまで自動（`release.yml`）。
- Windows：TwinKeys は CI で Microsoft Store 用の MSIX を作る（`windows.yml`、`docs/windows-store-submit.md`）。Microsoft の開発者登録は個人なら無料。

## 9. 持ち主とのやりとり

- 日本語で、短く、専門用語には一言説明を付ける。英語で返さない。
- 事実・推測・不明を分け、変わりうる情報は公式のページで確かめて URL を書く。
- サイトで操作が必要なときは、その画面の URL と、どこを押すかを書く。
