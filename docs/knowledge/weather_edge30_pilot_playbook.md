# Weather Edge-30 Pilot Playbook

## 案件名

- 正式名: **Weather Edge-30 Pilot**
- 略称: **W30P**
- 対象: weather系 Polymarket 市場の Top30 候補を observe-only で絞り込み、実践投入可否を判定する運用

## 目的

- no_longshot と lateprob の2系統スキャナを統合し、同時に拾える市場を優先して候補品質を上げる。
- 候補抽出と実践投入判断を分離し、運用判断を再現可能な基準で固定化する。

## 仕組み

1. `polymarket_no_longshot_observe.py screen` が NO側ロングショット候補を抽出する。
2. `polymarket_lateprob_observe.py screen` が満期近傍の高確率/低確率候補を抽出する。
3. `build_weather_consensus_watchlist.py` が2系統を市場単位でマージし、スコア順に並べる。
4. `top_n=30` の上位30件を運用上の監視候補として扱う。

スコアは以下の加重合成。

- overlap（両系統で一致）
- net_yield_per_day
- max_profit
- liquidity
- volume_24h

## キャッシュポイント定義

- Top30 は「発注」ではなく「監視優先順位」。
- 想定収益の基準は `entry_price -> payout(0/1)` の差分で、基本は満期決済ベース。
- 実キャッシュ化は次のいずれか。
  - 手動でのクローズ
  - 満期での自動確定

## 実践投入判定基準 (Hard Gates)

`scripts/judge_weather_top30_readiness.py` の既定値。

- `row_count >= 20`
- `both_ratio >= 0.70`
- `median_net_yield_per_day >= 0.05`
- `top10_avg_max_profit >= 0.07`
- `median_liquidity >= 700`
- `median_volume_24h >= 500`
- `median_hours_to_end <= 30`
- `meta.observe_only == true`
- `execution_plan_present == true`（実践投入判定時）
  - 既定では `logs/<profile_name>_execution_plan_latest.json` を検証対象とする
  - 代替で supervisor command に live 実行フラグがある場合でも通過扱い

判定ロジック:

- Hard Gate 全通過: `GO`
- 1つでも不通過: `NO_GO`

## ドライラン運用

1. 最新候補を更新する（observe-only）。
2. readiness 判定を実行する。
3. `logs/*_top30_readiness_latest.json` を運用判断の単一ソースにする。
4. 実践投入判断時は `logs/<profile_name>_execution_plan_latest.json` を最新化してから判定する。
5. 複数profileを運用する場合は `scripts/run_weather_top30_readiness_daily.ps1` で日次再判定と集計を自動化する。

コマンド例:

```powershell
python scripts/build_weather_consensus_watchlist.py `
  --no-longshot-csv logs/weather_7acct_auto_no_longshot_latest.csv `
  --lateprob-csv logs/weather_7acct_auto_lateprob_latest.csv `
  --profile-name weather_7acct_auto `
  --top-n 30 `
  --min-liquidity 500 `
  --min-volume-24h 100 `
  --score-mode edge `
  --out-csv logs/weather_7acct_auto_consensus_watchlist_latest.csv `
  --out-json logs/weather_7acct_auto_consensus_watchlist_latest.json

python scripts/judge_weather_top30_readiness.py `
  --consensus-json logs/weather_7acct_auto_consensus_watchlist_latest.json `
  --supervisor-config logs/bot_supervisor.weather_7acct_auto.observe.json `
  --execution-plan-file logs/weather_7acct_auto_execution_plan_latest.json `
  --pretty
```

## 運用メモ

- 本リポジトリは既定で observe-only。ライブ化は別プロセスの明示設計が必要。
- readiness で `NO_GO` の場合、まずは gate を落とした原因（流動性不足、利幅不足、execution plan 欠如）を修正する。
- 手動実行時の運用手順は `docs/knowledge/weather_edge30_manual_execution_checklist.md` を参照する。
