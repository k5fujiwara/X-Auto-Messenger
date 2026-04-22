import argparse
import logging
from typing import Optional

from env_loader import load_env_file
from main import _build_post_text, choose_article, fetch_articles, generate_x_summary, publish_with_hashtag_retry


load_env_file()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _confirm_execution() -> bool:
    answer = input("実際にXへ投稿します。続行する場合は 'yes' と入力してください: ").strip().lower()
    return answer == "yes"


def _select_article(source: Optional[str] = None):
    articles = fetch_articles()
    if not articles:
        logger.error("記事取得に失敗したため、テスト投稿を中止します。")
        return None

    if source:
        for article in articles:
            if article.source == source:
                return article
        logger.error("指定した source が見つかりません: %s", source)
        return None

    return choose_article(articles, "random")


def run_local_test_post(source: Optional[str] = None) -> bool:
    article = _select_article(source=source)
    if not article:
        return False

    logger.info("テスト投稿対象: source=%s title=%s", article.source, article.title)

    generated_post = generate_x_summary(article)
    if not generated_post:
        logger.error("Gemini の投稿文生成に失敗しました。")
        return False

    preview_text = _build_post_text(generated_post.body, generated_post.hashtags)
    logger.info("生成本文:\n%s", generated_post.body)
    logger.info("生成タグ:\n%s", "\n".join(generated_post.hashtags))
    logger.info("投稿プレビュー:\n%s", preview_text)
    logger.info("返信URL: %s", article.url)

    return publish_with_hashtag_retry(
        body=generated_post.body,
        hashtags=generated_post.hashtags,
        url=article.url,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="X 自動投稿のローカル手動テスト")
    parser.add_argument(
        "--source",
        choices=["hp", "note"],
        help="投稿対象のフィードを固定する",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="確認プロンプトを省略してそのまま投稿する",
    )
    args = parser.parse_args()

    if not args.yes and not _confirm_execution():
        logger.info("ユーザーによりテスト投稿を中止しました。")
        return 0

    success = run_local_test_post(source=args.source)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
