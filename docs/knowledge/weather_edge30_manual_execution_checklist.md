# Weather Edge-30 Manual Execution Checklist

## 目的

- `Weather Edge-30 Pilot (W30P)` の実践投入を、再現可能かつ安全側で手動実行するためのチェックリスト。
- 本チェックリストは投入判断補助であり、投資助言ではない。

## 前提

- readiness 判定が `strict` で `GO`。
  - `logs/<profile_name>_top30_readiness_strict_latest.json`
- 実行計画ファイルが最新化済み。
  - `logs/<profile_name>_execution_plan_latest.json`
- observe-only ロールバック経路が確認済み。
  - `logs/bot_supervisor.<profile_name>.observe.json`

## 実行前チェック (必須)

1. 日付と対象プロファイルを固定する（例: `weather_7acct_auto`）。
2. 以下4ファイルのタイムスタンプが直近であることを確認する。
   - `logs/<profile_name>_no_longshot_latest.csv`
   - `logs/<profile_name>_lateprob_latest.csv`
   - `logs/<profile_name>_consensus_watchlist_latest.json`
   - `logs/<profile_name>_top30_readiness_strict_latest.json`
3. readiness の hard gate を再確認する。
   - `decision == GO`
   - `execution_plan_present == true`
4. Top30 の偏りを確認する。
   - `both_ratio`
   - `median_net_yield_per_day`
   - `top10_avg_max_profit`
5. リスク上限を当日の運用メモに記録する。
   - `max_daily_loss_usd`
   - `max_open_positions`
   - `max_notional_per_market_usd`

## 実行時チェック (必須)

1. 実行開始時刻を記録する（UTC）。
2. 実行中は以下を定期監視する。
   - 実現/未実現の損益推移
   - オープンポジション数
   - 市場ごとの約定サイズ
3. しきい値到達時は即停止する。
   - 日次損失上限到達
   - 想定外の約定連鎖
   - データ/接続異常

## 停止とロールバック

1. 実行停止を宣言し、停止時刻を記録する（UTC）。
2. observe-only 監視に戻す。
   - `python scripts/bot_supervisor.py run --config logs/bot_supervisor.<profile_name>.observe.json`
3. 30分以内に事後ログを残す。
   - 何が予定通りだったか
   - 何が想定外だったか
   - 次回の閾値調整案

## 定例コマンド (observe-only)

```powershell
python scripts/report_weather_top30_readiness.py --pretty
```

```powershell
python scripts/judge_weather_top30_readiness.py `
  --consensus-json logs/<profile_name>_consensus_watchlist_latest.json `
  --supervisor-config logs/bot_supervisor.<profile_name>.observe.json `
  --execution-plan-file logs/<profile_name>_execution_plan_latest.json `
  --pretty
```
