# 地方ポツン AUTO v0.6.2 SERVER
SQLite database-is-locked 修正版。
- WALモード
- busy_timeout 30秒
- DB初期化を起動時1回だけに変更
- NAR取得中はDB接続を保持しない
- 予約/スナップショット書込みをLOCKで直列化
- 15/10/5分前のサーバー監視は継続
注意: Render Freeのスリープと /tmp DB の非永続性は別制約として残ります。
