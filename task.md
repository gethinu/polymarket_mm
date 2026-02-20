# Simmer 30-Day Observe A/B Task

Created: 2026-02-20  
Window: 2026-02-20 to 2026-03-22 (30 days)

## Goal

`simmer_pingpong_mm.py` の改善版が、observe-only で実運用候補として有効かを判定する。

判定に使う主要KPI:
- `turnover/day` (回転率)
- `median_hold` (在庫滞留時間)
- `expectancy/cycle` (1サイクル期待値の推定)
- `total pnl` / `pnl_today`（参考）

## Variants

### A: Baseline (現行基準)

- `--asset-quotas "none"` (空扱い)
- `--max-hold-sec 60`（短期比較データを作るための実験設定）
- `--sell-target-decay-cents-per-min 0.05`

### B: Candidate (改善版)

- `--asset-quotas "bitcoin:2,ethereum:1,solana:1"`
- `--max-hold-sec 20`
- `--sell-target-decay-cents-per-min 0.20`

## Fixed Controls (A/B共通)

- observe-only（`--execute` は使わない）
- `--paper-trades`（擬似約定で在庫回転を評価）
- `--paper-seed-every-sec 120`（flat時に2分ごとに擬似エントリー）
- `--public-tag crypto`
- `--auto-select-count 6`
- `--public-limit 400`
- `--prob-min 0.02 --prob-max 0.98`
- `--min-time-to-resolve-min 5`
- `--spread-cents 0.8`
- `--trade-shares 5`
- `--max-inventory-shares 10`
- `--min-trade-amount 0.25 --max-trade-amount 5`
- `--poll-sec 2 --quote-refresh-sec 60`
- `--metrics-sample-sec 5`
- `--summary-every-sec 0`
- `--daily-loss-limit-usd 5`

## Run Commands (Parallel Observe)

PowerShellで2本同時起動（別ログ/別state/別metrics）:

```powershell
Start-Process -FilePath python -ArgumentList @(
  'scripts/simmer_pingpong_mm.py',
  '--public-tag','crypto',
  '--auto-select-count','6',
  '--public-limit','400',
  '--min-time-to-resolve-min','5',
  '--prob-min','0.02','--prob-max','0.98',
  '--spread-cents','0.8',
  '--trade-shares','5',
  '--max-inventory-shares','10',
  '--min-trade-amount','0.25','--max-trade-amount','5',
  '--poll-sec','2','--quote-refresh-sec','60',
  '--metrics-sample-sec','5',
  '--summary-every-sec','0',
  '--daily-loss-limit-usd','5',
  '--paper-trades',
  '--paper-seed-every-sec','120',
  '--asset-quotas','none',
  '--max-hold-sec','60',
  '--sell-target-decay-cents-per-min','0.05',
  '--log-file','logs/simmer-ab-baseline.log',
  '--state-file','logs/simmer_ab_baseline_state.json',
  '--metrics-file','logs/simmer-ab-baseline-metrics.jsonl'
)

Start-Process -FilePath python -ArgumentList @(
  'scripts/simmer_pingpong_mm.py',
  '--public-tag','crypto',
  '--auto-select-count','6',
  '--public-limit','400',
  '--min-time-to-resolve-min','5',
  '--prob-min','0.02','--prob-max','0.98',
  '--spread-cents','0.8',
  '--trade-shares','5',
  '--max-inventory-shares','10',
  '--min-trade-amount','0.25','--max-trade-amount','5',
  '--poll-sec','2','--quote-refresh-sec','60',
  '--metrics-sample-sec','5',
  '--summary-every-sec','0',
  '--daily-loss-limit-usd','5',
  '--paper-trades',
  '--paper-seed-every-sec','120',
  '--asset-quotas','bitcoin:2,ethereum:1,solana:1',
  '--max-hold-sec','20',
  '--sell-target-decay-cents-per-min','0.20',
  '--log-file','logs/simmer-ab-candidate.log',
  '--state-file','logs/simmer_ab_candidate_state.json',
  '--metrics-file','logs/simmer-ab-candidate-metrics.jsonl'
)
```

停止:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object {
    $_.CommandLine -like '*simmer_pingpong_mm.py*' -and
    ($_.CommandLine -like '*simmer-ab-baseline*' -or $_.CommandLine -like '*simmer-ab-candidate*')
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## Daily Reporting (固定時刻で比較)

毎日 00:05 ローカルで前日分を集計する。

```powershell
$since = (Get-Date).Date.AddDays(-1).ToString('yyyy-MM-dd HH:mm:ss')
$until = (Get-Date).Date.ToString('yyyy-MM-dd HH:mm:ss')

python scripts/report_simmer_observation.py `
  --metrics-file logs/simmer-ab-baseline-metrics.jsonl `
  --log-file logs/simmer-ab-baseline.log `
  --state-file logs/simmer_ab_baseline_state.json `
  --since $since --until $until

python scripts/report_simmer_observation.py `
  --metrics-file logs/simmer-ab-candidate-metrics.jsonl `
  --log-file logs/simmer-ab-candidate.log `
  --state-file logs/simmer_ab_candidate_state.json `
  --since $since --until $until
```

Scheduler登録（毎日 00:05 実行）:

```powershell
$tr = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Repos\polymarket_mm\scripts\run_simmer_ab_daily_report.ps1"'
schtasks /Create /TN "SimmerABDailyReport" /SC DAILY /ST 00:05 /TR $tr /F
schtasks /Run /TN "SimmerABDailyReport"
schtasks /Query /TN "SimmerABDailyReport" /V /FO LIST
```

日次比較の最新サマリは以下に保存される:
- `logs/simmer-ab-daily-compare-latest.txt`
- `logs/simmer-ab-daily-compare-history.jsonl` (日次1行の履歴)

Discordに日次比較を通知する場合（Webhook設定済み前提）:

```powershell
[Environment]::SetEnvironmentVariable('SIMMER_AB_DAILY_COMPARE_DISCORD','1','User')
```

履歴の推移確認:

```powershell
python scripts/report_simmer_ab_trend.py --days 30 --last 14
```

## Decision Rule (2026-03-22)

Candidateを採用する条件:

1. `turnover/day` が Baseline 以上
2. `median_hold` が Baseline より短い
3. `expectancy/cycle` が Baseline を有意に下回らない（悪化が 10% 未満）
4. エラー頻度（halt/errors）が Baseline と同等以下

上記を満たさない場合は、`asset_quotas` / `max_hold_sec` / `sell_target_decay` を再調整して次の30日を開始する。

注記:
- 日次比較で `Decision: INSUFFICIENT` は約定データ不足を意味する（A/Bのどちらかで `buy=0` または `sell=0`、あるいは `closed_cycles=0`）。採否判定には使わない。

## Quick Size Sweep (2026-02-20, observe+paper)

目的:
- 「資金（サイズ）を増やしたときに、実際に優位性が伸びるか」を短時間で確認する。

条件:
- 共通ロジックは固定（A/B差分は `asset_quotas` / `max_hold_sec` / `sell_decay` のみ）。
- `trade_shares` / `max_inventory_shares` / `max_trade_amount` を段階的に変更。

結果（抜粋）:
- `shares=5`（`max_inv=10`, `max_amount=5`）
  - Window: `2026-02-20 20:41:57 -> 20:53:57`
  - Decision: `PASS`
  - Delta(C-B): `turnover/day=+1680`, `median_hold_sec=-39.5`
- `shares=10`（`max_inv=20`, `max_amount=10`）
  - Window: `2026-02-20 21:44:32 -> 21:51:44`
  - Decision: `PASS`
  - Delta(C-B): `turnover/day=+800`, `median_hold_sec=-37.5`
- `shares=20`（`max_inv=40`, `max_amount=20`）
  - Window: `2026-02-20 21:47:58 -> 21:55:10`
  - Decision: `PASS`
  - Delta(C-B): `turnover/day=+800`, `median_hold_sec=-39.5`

解釈（現時点）:
- サイズを 10->20 に増やしても、A/B差分の `turnover/day` はほぼ頭打ち（+800 付近）。
- 少なくともこの短時間・paper条件では「サイズ増 = 儲け増」の単純比例は確認できない。

### Extended check (`shares=20`, run: `ab20r`)

同一設定で継続観測し、短窓ノイズを確認:

- Window: `2026-02-20 22:35:17 -> 22:41:17`  
  - Decision: `FAIL`（`turnover` gateのみNG）  
  - Delta(C-B): `turnover/day=-480`, `median_hold_sec=-38.0`
- Window: `2026-02-20 22:31:39 -> 22:43:39`  
  - Decision: `PASS`  
  - Delta(C-B): `turnover/day=+480`, `median_hold_sec=-38.0`

示唆:
- `shares=20`では短窓（~6分）でturnoverの符号が反転する程度にブレる。
- ただし `median_hold` は一貫して Candidate が優位（約 -38秒）。
