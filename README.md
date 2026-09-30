# 地方ポツン AUTO v0.3
NARの公開オッズページを取得し、単勝支持から説明しにくい馬連・馬単・三連単の歪みを試験的にスコア化する研究用Webアプリです。

## Render
Build: `pip install -r requirements.txt`
Start: `gunicorn app:app`

注意: NAR側ページ構造の変更やアクセス制限により取得が停止する可能性があります。指数は実験的指標で、的中や収益を保証しません。
