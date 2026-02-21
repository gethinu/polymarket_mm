# Expert Panel Report (consult)

Date: 2026-02-20  
Mode: consult  
Trigger: 「MMの専門家・勝っている人の視点で、実用化に向けた改善案を出す」  
Focus files:
- `scripts/simmer_pingpong_mm.py`
- `logs/simmer-pingpong.log`
- `docs/SIMMER_PINGPONG.md`
- `docs/INTERFACES.md`
- `docs/ARCHITECTURE.md`
- `docs/STATE.md`
- `README.md`

Note:
- User指示により、`doc/archive/workflows/expert-panel.md` / `doc/owners_guide.md` / `doc/SYSTEM_ARCHITECTURE.md` の未存在は無視して実施。
- 参考として `docs/ARCHITECTURE.md` を確認。

## Preflight

Git status (`git status --short`):
- `README.md` (M)
- `docs/INTERFACES.md` (M)
- `docs/STATE.md` (M)
- `scripts/bitflyer_mm_observe.py` (M)
- `scripts/optimize_bitflyer_mm_params.py` (M)
- `scripts/report_bitflyer_mm_observation.py` (M)
- `scripts/simmer_pingpong_mm.py` (M)
- `scripts/fade_monitor_dashboard.py` (??)
- `scripts/polymarket_clob_fade_observe.py` (??)
- `scripts/report_clob_fade_observation.py` (??)

Git log (`git log -n 5 --oneline --decorate`):
1. `9979d24` Improve Simmer automation safety and add observe tooling
2. `db0df4c` Document Simmer observation report in README
3. `1dc4980` Enforce event-only Discord notifications
4. `3429838` Limit Discord notifications to event-driven signals
5. `82f4407` Update architecture docs and set terminal cwd

## Baseline Facts (from code/logs)

- observe-onlyが原則だが、環境変数でLIVE化可能。
  - `scripts/simmer_pingpong_mm.py:12`
  - `scripts/simmer_pingpong_mm.py:57`
  - `scripts/simmer_pingpong_mm.py:396`
  - `scripts/simmer_pingpong_mm.py:424`
- Universeはデフォルト `auto-select-count=3`, `public-tag=crypto`。
  - `scripts/simmer_pingpong_mm.py:769`
  - `scripts/simmer_pingpong_mm.py:771`
- エントリー/イグジット条件は単純な閾値クロス。
  - BUY: `scripts/simmer_pingpong_mm.py:550`
  - SELL: `scripts/simmer_pingpong_mm.py:644`
- 2026-02-17 に oversize系の在庫が1回発生。
  - `logs/simmer-pingpong.log:814`
- 2026-02-18 に1回転の利益確定は発生。
  - BUY: `logs/simmer-pingpong.log:955`
  - SELL: `logs/simmer-pingpong.log:957`
- 同セッションでSELL失敗（rate limit）を短時間連発。
  - `logs/simmer-pingpong.log:956`
  - `logs/simmer-pingpong.log:971`
- 直近は entry band 外で `BUY skipped` が高頻度。
  - `logs/simmer-pingpong.log:1717`
  - `logs/simmer-pingpong.log:1847`
- stale marketは削除せず inactive 化してPnL連続性を保持。
  - `scripts/simmer_pingpong_mm.py:477`

## Consult Topics

### Topic A: Universe設計（BTC偏重をどう崩すか）

Option A1: 現状維持（`crypto` + top score）
- Tradeoff: 実装コスト最小。だがBTC過密帯に吸われ、同質市場ばかり掴む。
- 反証/対抗意見: 「市場数が多いBTCが最適」は短期は正しいが、出口不足時は在庫相関で逆効果。

Option A2: 資産クォータ導入（BTC/ETH/SOL最低枠）
- Tradeoff: シグナル多様化で在庫偏りを減らせる。だが流動性が薄い資産を掴むとスリッページ悪化。
- 反証/対抗意見: 「薄い市場は捨てるべき」。ただし完全排除だと学習データ不足で最適化が進まない。

Option A3: クォータ + 流動性/解像度フィルタ（ハイブリッド）
- Tradeoff: 分散と実行可能性のバランス。実装は中程度。
- 反証/対抗意見: ルールが複雑になりブラックボックス化しやすい。監査ログ必須。

### Topic B: 在庫回転（持つだけで終わらせない）

Option B1: 現状維持（価格クロス待ち）
- Tradeoff: ロジックは単純。だが売りトリガー不足時に在庫が寝る。
- 反証/対抗意見: 「待てば戻る」は期限付き市場では成立しにくい。

Option B2: 時間減衰exit（max_hold + sell_targetを時間で下げる）
- Tradeoff: 回転率は上がる。平均利益幅は下がる可能性。
- 反証/対抗意見: 早売りで期待値を削る懸念。ただし未決済リスクを定量化できる利点が大きい。

Option B3: 在庫圧縮モード（inventory比率で片側停止・反対側優先）
- Tradeoff: 在庫リスクは下がる。機会損失は増える。
- 反証/対抗意見: 「機会損失が痛い」。だが現状は機会より在庫滞留の損失が目立つ。

### Topic C: 運用ガード（LIVE誤起動とノイズ）

Option C1: 環境変数優先のまま運用で回避
- Tradeoff: 実装不要。ヒューマンエラーに弱い。
- 反証/対抗意見: 「運用でカバー」は既に破綻した実績がある。

Option C2: `--execute` 明示時のみLIVE許可（envは補助）
- Tradeoff: 安全性が高い。既存運用スクリプトの互換調整が必要。
- 反証/対抗意見: 自動化が面倒になる。だが事故コストより小さい。

Option C3: 起動時プロファイル固定（observe/live profileファイル）
- Tradeoff: 再現性が高い。ファイル管理が増える。
- 反証/対抗意見: 設定分散の懸念。単一プロファイル管理で解決可能。

## Expert Opinions (MM / Polymarket specialists, deliberately harsh)

### 1) CLOBマーケットメイク責任者（HFT）
- 「売れない在庫はエッジじゃない」。BUYの機会に対してSELL成立が弱すぎる。実運用でSELL失敗が連打 (`logs/simmer-pingpong.log:956`, `logs/simmer-pingpong.log:971`)。
- `allow_sell` はあるが、在庫圧縮優先ロジックが薄い (`scripts/simmer_pingpong_mm.py:644`, `scripts/simmer_pingpong_mm.py:646`)。
- 推奨: B3。inventory pressureで新規BUYを抑え、exit優先にする。

### 2) LMSR/AMM専門MM
- AMMでCLOB的「固定スプレッド往復」をそのまま当てるのは雑。価格応答が板の厚みと違う。
- `spread_cents` 固定で回すより、状態依存にしないと尾で刺さる (`scripts/simmer_pingpong_mm.py:519`, `scripts/simmer_pingpong_mm.py:776`)。
- 推奨: B2 + ボラ依存スプレッド（最低/最大帯付き）。

### 3) 予測市場イベントトレーダー（短期）
- 満期近辺はジャンプが支配する。`prob_min/prob_max` だけでは足りない。実際に0.005帯でskip連打が発生 (`logs/simmer-pingpong.log:1717`, `logs/simmer-pingpong.log:1847`)。
- `min_time_to_resolve_min` はあるが、相場遷移速度への適応がない (`scripts/simmer_pingpong_mm.py:769`, `scripts/simmer_pingpong_mm.py:773`)。
- 推奨: A3。満期近辺の閾値を動的に厳しくする。

### 4) Polymarketヘビートレーダー（裁量）
- 「市場を増やせば勝てる」は甘い。いま必要なのは市場数ではなく回転率。
- `auto-select-count=3` は少ないが、本質は「出口が詰まる市場を先に捨てる」こと (`scripts/simmer_pingpong_mm.py:769`, `scripts/simmer_pingpong_mm.py:477`)。
- 推奨: A3 + B2。資産分散 + 保有時間制限を同時導入。

### 5) Polymarketボット運用者（実戦）
- envでLIVE化できる設計は事故誘発。実際に運用でLIVE混入した (`scripts/simmer_pingpong_mm.py:57`, `scripts/simmer_pingpong_mm.py:396`, `scripts/simmer_pingpong_mm.py:792`)。
- 推奨: C2。LIVEはCLI明示のみ。env単独LIVE禁止。

### 6) ポジションリスク管理者（デルタ中立志向）
- `max_inventory_shares` はあるが、在庫の年齢管理がない。古い在庫が残る構造。
- 典型例が `FILL BUY 41.26` の尾在庫 (`logs/simmer-pingpong.log:814`)。
- 推奨: B2。`max_hold_sec` と time-decay exit を必須化。

### 7) 実行系エンジニア（API/レート制限）
- 「失敗してからクールダウン」は後手。ログに同一エラーが短時間に密集している (`logs/simmer-pingpong.log:958`, `logs/simmer-pingpong.log:966`)。
- `trade_cooldowns` は入っているが、side/market単位の状態遷移が粗い (`scripts/simmer_pingpong_mm.py:496`, `scripts/simmer_pingpong_mm.py:686`)。
- 推奨: 失敗時の再試行ポリシーを指数バックオフ化し、失敗理由別に分岐。

### 8) 解決・精算フロー専門（Polymarket運用）
- resolved市場の残ポジションは「戦略評価」を汚す。実口座とbot stateの差分管理が弱い。
- staleをinactive保持する方針は妥当だが、実口座照合フローが欠ける (`scripts/simmer_pingpong_mm.py:477`, `docs/STATE.md:21`)。
- 推奨: 日次の`/positions`照合ジョブ + 差分アラート。

### 9) トレーディングSRE（本番信頼性）
- 1時間summaryはあるが、KPIが粗い。`expectancy/hold-time/turnover`が見えないと改善不能。
- 既存reportは観測中心で、意思決定KPIが足りない (`scripts/report_simmer_observation.py`, `docs/INTERFACES.md:36`)。
- 推奨: 評価KPIを追加し、A/Bを30日で強制比較。

## Musk Final Decision

### Do (やること)
1. A3を採用: 資産クォータ + 流動性フィルタのUniverse選定。
2. B2を採用: `max_hold_sec` + `sell_target` 時間減衰で在庫回転率を強制改善。
3. C2を採用: LIVEは `--execute --confirm-live YES` の明示時のみ。env単独LIVEは無効化。
4. 30日ベンチ: `observe`でシミュレートし、期待値/回転率/ドローダウンで判定。

### Don't (やらないこと)
1. 「通貨を増やすだけ」で勝てる前提を置かない。
2. 1-2回の約定結果で優位性を主張しない。
3. 運用ルールに依存した安全管理を続けない。

## Actionable Items

1. Universe拡張:
- `scripts/simmer_pingpong_mm.py` に `--asset-quotas` 相当（BTC/ETH/SOL最小枠）を追加。
- 選定時に `divergence` と `resolves_at` を併用し、同質市場の偏りを抑制。

2. 在庫回転ガード:
- `scripts/simmer_pingpong_mm.py` に `--max-hold-sec`、`--sell-target-decay-cents-per-min` を追加。
- `MarketState` に `last_fill_ts` を持たせる。

3. LIVE安全化:
- `scripts/simmer_pingpong_mm.py` の `args.execute` 判定をCLI優先に変更し、env単独のLIVE化を禁止。
- `docs/INTERFACES.md` と `docs/SIMMER_PINGPONG.md` に新ルールを明記。

4. 評価運用:
- `scripts/report_simmer_observation.py` を拡張して、`expectancy`, `median_hold`, `turnover/day` を出力。
- 日次で `logs/` から自動集計し、同一条件比較を固定。
