# アイカツ！アンコール 取扱店舗の見張り

公式サイトの[取扱店舗](https://dcd.aikatsu.com/encore/shoplist/)で、決めた店の筐体の「最終更新」が変わったら Discord に知らせます。
GitHub Actions で 10 分おきに動きます（GitHub の都合で数分〜20 分ほど遅れることがあります）。PC を切っていても動きます。

## 届く通知
- 👀 見張りを始めました：初めて調べた店の今の状態
- 🔔 最終更新が変わりました：何台目が、いつからいつに変わったか、排出されるカード（弾）
- 🆕 台が増えた／➖ 台が消えた
- ⚠️ 店が見つからない（店名の打ち間違いなど。1 回だけ）

## 設定（GitHub の Settings → Secrets and variables → Actions → Secrets）
| 名前 | 中身 |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord のウェブフックの URL |
| `STORES` | 見張る店名。1 行に 1 店。公式サイトの検索欄と同じで、店名の一部でも探せる（完全に同じ名前の店があればその店だけ） |

店名は Secrets に入れるので、公開リポジトリのログには出ません。`state.json` にも店名は書かず、ハッシュだけを書きます。

## 動作確認
Actions タブ →「取扱店舗の最終更新を見張る」→「Run workflow」。変化がなくても今の状態が Discord に届きます。

## この PC で試す（Discord には送らない）
```bash
DRY_RUN=1 STATE_FILE=test_state.json STORES="店名" py -3 -X utf8 watch.py
```

## 注意
- 公開リポジトリは無料で何回でも動かせます（非公開だと月の無料枠を超えます）。
- 60 日間リポジトリに更新がないと定期実行が止まりますが、最終更新が変わるたびに `state.json` を保存するので、普段は止まりません。止まったら Actions タブで有効にし直します。
