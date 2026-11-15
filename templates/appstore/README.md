# App Store の自動登録（TwinKeys Remote で使った実物）

`kajisho5/ableton-twinkeys` から 2026-11-15 に写したもの。使い方と注意は `docs/playbook.md` の 4 章。

- `asc_appstore.py`：アプリの `scripts/` に置く。「TwinKeys Remote」と書いてある所を書き換える（`grep -n TwinKeys asc_appstore.py`）。
- `asc_beta.py`：TestFlight の外部テスト。
- `appstore-prepare.yml`：アプリの `.github/workflows/` に置く。ブランチ名・既定のバージョン・ビルド番号・価格を書き換える。
- `listing.example.json`：アプリの `docs/appstore/listing.json` として置き、文面を全部書き換える。
