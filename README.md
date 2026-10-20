# ODDS SCOPE NAR（potun-auto）

地方競馬（NAR）専用。発走15・10・5・4・3分前のオッズ（全6券種）を自動取得し、FINAL候補の検証・学習を行う。

## 自動で動くもの
| 処理 | 内容 |
|---|---|
| 全レース自動予約 | 公式「本日のレース情報」から当日・翌日の全場全レースを10分ごとに取り込み。発走時刻の変更に追従、公式の取止は予約を取消。ユーザーが取り消した予約は復活させない |
| オッズ取得 | 15/10/5/4/3分前。取得窓は `capture_timing.py`。窓を過ぎた取得は破棄し、後から補完しない |
| 結果・払戻 | 公式の着順と払戻金を保存（同着対応）。払戻公表前の暫定着順では確定しない（20分経っても払戻が出なければ着順のみ保存し後で補完） |
| 確定オッズ | 発走10分後以降に単勝・複勝の確定オッズを保存（3分前→確定の分析用） |
| 学習 | 結果確定で学習サンプル生成。再学習は30分に1回まとめて実行（Render FreeのCPU保護） |
| 常時起動 | GitHub Actions `keepalive`（8〜22時）。停止・取得漏れでジョブ失敗→メール通知 |
| バックアップ | GitHub Actions `db-backup`（毎日5:30）。AES-256暗号化して90日保管 |

## 診断・評価API
- `/api/capture-health?date=YYYY-MM-DD` … レースごとの5時点の成否と未取得理由、成功率、自動予約の状態
- `/api/capture-log?race_key=...` … 個別レースの詳細ログ
- `/api/roi` … 公式払戻ベースの回収率（FINAL上位3 vs 人気上位3、95%区間）と、3分前→確定の動き別成績
- `/api/learning` … 3着内率と学習状況

## バックアップの設定（1回だけ）
GitHub → Settings → Secrets and variables → Actions に次の2つを登録：
- `BACKUP_DATABASE_URL` … Render の PostgreSQL「External Database URL」
- `BACKUP_PASSPHRASE` … 任意の長い合言葉（復元に必要。紛失すると復元不可）

未設定の間はバックアップをスキップし、警告だけ出します。

### 復元
```
gpg -d odds-scope-YYYY-MM-DD.dump.gpg > db.dump
pg_restore --no-owner -d "$NEW_DATABASE_URL" db.dump
```

## Render Free の制約（保証できない点）
- 15分間アクセスがないと停止する。`keepalive` で防ぐが、Render 側の再起動・デプロイ中（約1分）は取得できない
- 開催中のデプロイは避ける
- Render 無料 PostgreSQL は作成30日で期限切れ（その14日後に削除）。DBの提供元と期限を確認すること

## テスト
```
python -m unittest test_reliability test_scheduler test_race3d test_capture_health test_results_parser test_auto_schedule_roi test_pg_integration
node test_ui.cjs
```
`test_pg_integration` は使い捨てDB（`ODDS_SCOPE_TEST_DB=1`）でのみ実行。`fixtures/` は公式ページ（2026-10-20）の抜粋。
