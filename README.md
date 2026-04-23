# X-Auto-Messenger

RSS から最新記事を取得し、Gemini で X 向け本文とハッシュタグを生成して、X へ自動投稿するツールです。

親ポストとして本文を投稿し、その返信として記事 URL を投稿します。GitHub Actions を使った定期実行にも対応しています。

## 機能

- `https://info-study.com/feed` と `https://note.com/k5fujiwara/rss` から最新記事を取得
- `random` または `alternate` で投稿対象記事を選択
- Gemini で本文とハッシュタグ 5〜6 個を生成
- 文字数超過時はタグを 1 つずつ減らして再投稿
- 親ポスト投稿後、返信で URL を投稿
- 投稿直前に `0〜900` 秒のジッターを挿入
- 日本時間 `8:00〜24:00` の範囲で不規則投稿
- ローカル `.env` と GitHub Secrets / Variables の両対応

## 動作ファイル

- `main.py`: 記事取得、Gemini 生成、投稿判定、投稿実行
- `x_publisher.py`: X API v2 への投稿処理
- `env_loader.py`: `.env` 読み込み
- `heck_models.py`: 利用可能な Gemini モデル確認
- `.github/workflows/post-to-x.yml`: GitHub Actions 定期実行

## 必要環境

- Python 3.11 以上推奨
- X API の認証情報
- Gemini API キー

依存モジュール:

```bash
pip install -r requirements.txt
```

## 環境変数

ローカルでは `.env`、GitHub では Secrets / Variables に設定します。

```env
X_API_KEY="..."
X_API_SECRET="..."
X_ACCESS_TOKEN="..."
X_ACCESS_SECRET="..."
X_BEARER_TOKEN="..."

GEMINI_API_KEY="..."
GEMINI_MODELS="gemini-3.1-pro-preview,gemini-2.5-flash,gemini-2.5-flash-lite"

ARTICLE_SELECTION_MODE="random"
POST_WINDOW_START_HOUR="8"
POST_WINDOW_END_HOUR="24"
MIN_POSTS_PER_DAY="6"
MAX_POSTS_PER_DAY="9"
POST_CHECK_INTERVAL_MINUTES="5"
MIN_GAP_MINUTES="60"
POST_SLOT_GRACE_MINUTES="20"
POST_SCHEDULE_SEED="change-me"
```

### 主な設定項目

- `ARTICLE_SELECTION_MODE`
  - `random`: 2つのフィードからランダム選択
  - `alternate`: 前回と別ソースを優先
- `GEMINI_MODELS`
  - 左から順に試行
  - 失敗時は次のモデルへフォールバック
- `POST_WINDOW_START_HOUR` / `POST_WINDOW_END_HOUR`
  - 日本時間ベースの投稿可能時間帯
- `MIN_POSTS_PER_DAY` / `MAX_POSTS_PER_DAY`
  - 1日の投稿回数レンジ
- `POST_CHECK_INTERVAL_MINUTES`
  - 何分ごとに投稿判定するか
- `MIN_GAP_MINUTES`
  - 投稿間の最低間隔
- `POST_SLOT_GRACE_MINUTES`
  - 投稿枠を過ぎたあと何分まで補足するか
- `POST_SCHEDULE_SEED`
  - 日ごとのランダムスケジュール生成用の種

## ローカル実行

```bash
python main.py
```

`main.py` は現在時刻がその日の投稿スロットに当たっている時だけ投稿します。投稿されなかった回は終了コード `1` で終了するため、GitHub Actions 上では success になりません。

Gemini の利用可能モデル確認:

```bash
python heck_models.py
```

## 投稿ロジック

1. 日本時間基準で、その日の投稿スロットをランダム生成
2. 現在時刻が対象スロットなら処理続行
3. RSS から最新記事を取得
4. Gemini で本文とハッシュタグを生成
5. 140字に収まらない場合はタグ数を減らして再試行
6. 親ポストを投稿
7. 返信として記事 URL を投稿

## GitHub Actions

workflow ファイル: `.github/workflows/post-to-x.yml`

GitHub Actions は UTC で次の cron を使っています。

- `*/5 23 * * *`
- `*/5 0-14 * * *`

これは日本時間では `8:00〜23:55` の 5 分ごとです。

### GitHub 側で設定する値

Secrets:

- `GEMINI_API_KEY`
- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_SECRET`
- `POST_SCHEDULE_SEED`

Variables:

- `GEMINI_MODELS`
- `ARTICLE_SELECTION_MODE`
- `POST_WINDOW_START_HOUR`
- `POST_WINDOW_END_HOUR`
- `MIN_POSTS_PER_DAY`
- `MAX_POSTS_PER_DAY`
- `POST_CHECK_INTERVAL_MINUTES`
- `MIN_GAP_MINUTES`
- `POST_SLOT_GRACE_MINUTES`

## 注意事項

- `.env` は公開しないでください
- すでに外部共有している場合、X API キーの再発行を推奨します
- GitHub Actions の定期実行は数分ずれることがあります
- X 側の仕様変更やレート制限により挙動が変わる可能性があります

## 今後の拡張候補

- 投稿済み記事の重複防止
- 手動テスト用のドライランモード
- 投稿ログの保存強化
- 失敗時の通知連携
