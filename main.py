import html
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import feedparser
import google.generativeai as genai

from env_loader import load_env_file
from x_publisher import has_recent_feed_reply, publish_to_x_detailed, was_url_recently_posted


load_env_file()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

FEEDS = {
    "hp": "https://info-study.com/feed",
    "note": "https://note.com/k5fujiwara/rss",
}
DEFAULT_SELECTION_MODE = "random"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
DEFAULT_GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
]
JST = timezone(timedelta(hours=9))
DEFAULT_POST_WINDOW_START_HOUR = 8
DEFAULT_POST_WINDOW_END_HOUR = 24
DEFAULT_MIN_POSTS_PER_DAY = 6
DEFAULT_MAX_POSTS_PER_DAY = 9
DEFAULT_POST_CHECK_INTERVAL_MINUTES = 5
DEFAULT_MIN_GAP_MINUTES = 60
DEFAULT_POST_SLOT_GRACE_MINUTES = 30
STATE_FILE = Path(__file__).with_name(".article_selection_state.json")
HASHTAG_HINTS = [
    "#教育",
    "#学び",
    "#勉強法",
    "#仕組み化",
    "#習慣化",
    "#成長",
]


@dataclass
class Article:
    source: str
    title: str
    url: str
    content: str


@dataclass
class GeneratedPost:
    body: str
    hashtags: list[str]


@dataclass
class PostingSchedule:
    start_hour: int
    end_hour: int
    check_interval_minutes: int
    min_gap_minutes: int
    slot_indexes: list[int]


@dataclass
class PostingOpportunity:
    slot_index: int
    slot_time_label: str
    grace_minutes: int
    minutes_since_slot: int


def _clean_text(value: str) -> str:
    if not value:
        return ""

    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_entry_content(entry) -> str:
    content_blocks = entry.get("content", [])
    if content_blocks:
        joined = " ".join(block.get("value", "") for block in content_blocks)
        return _clean_text(joined)

    summary = entry.get("summary", "")
    if summary:
        return _clean_text(summary)

    description = entry.get("description", "")
    return _clean_text(description)


def fetch_latest_article(source: str, feed_url: str) -> Optional[Article]:
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:
        logger.exception("Failed to fetch feed for %s: %s", source, exc)
        return None

    if getattr(parsed, "bozo", 0):
        logger.warning("Feed parsing reported issues for %s: %s", source, parsed.bozo_exception)

    entries = getattr(parsed, "entries", [])
    if not entries:
        logger.error("No entries found for feed: %s", source)
        return None

    entry = entries[0]
    title = _clean_text(entry.get("title", ""))
    url = entry.get("link", "").strip()
    content = _extract_entry_content(entry)

    if not title or not url:
        logger.error("Latest entry for %s is missing title or url.", source)
        return None

    logger.info("Fetched latest article from %s: %s", source, title)
    return Article(source=source, title=title, url=url, content=content)


def fetch_articles() -> list[Article]:
    articles = []
    for source, feed_url in FEEDS.items():
        article = fetch_latest_article(source, feed_url)
        if article:
            articles.append(article)

    if not articles:
        logger.error("Failed to fetch articles from all feeds.")
    return articles


def _load_last_source() -> Optional[str]:
    if not STATE_FILE.exists():
        return None

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("last_source")
    except Exception as exc:
        logger.warning("Failed to read selection state: %s", exc)
        return None


def _save_last_source(source: str) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps({"last_source": source}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to save selection state: %s", exc)


def choose_article(articles: list[Article], mode: str = DEFAULT_SELECTION_MODE) -> Optional[Article]:
    if not articles:
        return None

    normalized_mode = (mode or DEFAULT_SELECTION_MODE).strip().lower()
    if normalized_mode == "alternate" and len(articles) > 1:
        last_source = _load_last_source()
        for article in articles:
            if article.source != last_source:
                _save_last_source(article.source)
                logger.info("Selected article by alternate mode: %s", article.source)
                return article

    selected = random.choice(articles)
    _save_last_source(selected.source)
    logger.info("Selected article by %s mode: %s", normalized_mode, selected.source)
    return selected


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid integer for %s: %s. Falling back to %s.", name, raw_value, default)
        return default


def _build_daily_posting_schedule(target_date: date) -> PostingSchedule:
    start_hour = _get_int_env("POST_WINDOW_START_HOUR", DEFAULT_POST_WINDOW_START_HOUR)
    end_hour = _get_int_env("POST_WINDOW_END_HOUR", DEFAULT_POST_WINDOW_END_HOUR)
    check_interval_minutes = _get_int_env(
        "POST_CHECK_INTERVAL_MINUTES",
        DEFAULT_POST_CHECK_INTERVAL_MINUTES,
    )
    min_posts_per_day = _get_int_env("MIN_POSTS_PER_DAY", DEFAULT_MIN_POSTS_PER_DAY)
    max_posts_per_day = _get_int_env("MAX_POSTS_PER_DAY", DEFAULT_MAX_POSTS_PER_DAY)
    min_gap_minutes = _get_int_env("MIN_GAP_MINUTES", DEFAULT_MIN_GAP_MINUTES)

    if end_hour <= start_hour:
        logger.warning("POST_WINDOW_END_HOUR must be greater than POST_WINDOW_START_HOUR. Using defaults.")
        start_hour = DEFAULT_POST_WINDOW_START_HOUR
        end_hour = DEFAULT_POST_WINDOW_END_HOUR

    if check_interval_minutes <= 0:
        logger.warning("POST_CHECK_INTERVAL_MINUTES must be positive. Using default.")
        check_interval_minutes = DEFAULT_POST_CHECK_INTERVAL_MINUTES

    if min_gap_minutes < check_interval_minutes:
        min_gap_minutes = check_interval_minutes

    window_minutes = (end_hour - start_hour) * 60
    total_slots = max(1, window_minutes // check_interval_minutes)
    min_gap_slots = max(1, (min_gap_minutes + check_interval_minutes - 1) // check_interval_minutes)
    max_feasible_posts = 1 + ((total_slots - 1) // min_gap_slots)

    min_posts = max(1, min_posts_per_day)
    max_posts = max(min_posts, max_posts_per_day)
    if min_posts > max_feasible_posts:
        logger.warning("MIN_POSTS_PER_DAY is too high for the current window. Clamping to %s.", max_feasible_posts)
        min_posts = max_feasible_posts
    if max_posts > max_feasible_posts:
        logger.warning("MAX_POSTS_PER_DAY is too high for the current window. Clamping to %s.", max_feasible_posts)
        max_posts = max_feasible_posts
    if min_posts > max_posts:
        min_posts = max_posts

    schedule_seed = os.environ.get("POST_SCHEDULE_SEED", "").strip()
    rng = random.Random(
        f"{target_date.isoformat()}:{schedule_seed}:{start_hour}:{end_hour}:{min_posts}:{max_posts}:{min_gap_slots}"
    )
    post_count = rng.randint(min_posts, max_posts)

    slot_indexes = []
    for index in range(post_count):
        remaining_posts = post_count - index - 1
        earliest = 0 if not slot_indexes else slot_indexes[-1] + min_gap_slots
        latest = total_slots - 1 - (remaining_posts * min_gap_slots)
        if latest < earliest:
            latest = earliest
        slot_indexes.append(rng.randint(earliest, latest))

    return PostingSchedule(
        start_hour=start_hour,
        end_hour=end_hour,
        check_interval_minutes=check_interval_minutes,
        min_gap_minutes=min_gap_minutes,
        slot_indexes=slot_indexes,
    )


def _format_slot_time(slot_index: int, start_hour: int, check_interval_minutes: int) -> str:
    total_minutes = (start_hour * 60) + (slot_index * check_interval_minutes)
    hour = total_minutes // 60
    minute = total_minutes % 60
    return f"{hour:02d}:{minute:02d}"


def get_posting_opportunity(now: Optional[datetime] = None) -> Optional[PostingOpportunity]:
    current_time = now.astimezone(JST) if now else datetime.now(JST)
    schedule = _build_daily_posting_schedule(current_time.date())
    scheduled_times = [
        _format_slot_time(slot_index, schedule.start_hour, schedule.check_interval_minutes)
        for slot_index in schedule.slot_indexes
    ]

    logger.info(
        "Today's JST posting slots: %s",
        ", ".join(scheduled_times),
    )

    current_minutes = (current_time.hour * 60) + current_time.minute
    window_start_minutes = schedule.start_hour * 60
    window_end_minutes = schedule.end_hour * 60
    if current_minutes < window_start_minutes or current_minutes >= window_end_minutes:
        logger.info("Current JST time %s is outside the posting window.", current_time.strftime("%H:%M"))
        return None

    configured_grace_minutes = _get_int_env("POST_SLOT_GRACE_MINUTES", DEFAULT_POST_SLOT_GRACE_MINUTES)
    grace_minutes = max(schedule.check_interval_minutes, configured_grace_minutes)
    if schedule.min_gap_minutes > 1:
        grace_minutes = min(grace_minutes, schedule.min_gap_minutes - 1)

    matched_slot_index = None
    minutes_since_slot = None
    for slot_index in schedule.slot_indexes:
        slot_minutes = window_start_minutes + (slot_index * schedule.check_interval_minutes)
        elapsed = current_minutes - slot_minutes
        if 0 <= elapsed < grace_minutes:
            matched_slot_index = slot_index
            minutes_since_slot = elapsed

    if matched_slot_index is None or minutes_since_slot is None:
        logger.info(
            "Current JST time %s did not match any posting slot within the %s-minute grace window.",
            current_time.strftime("%H:%M"),
            grace_minutes,
        )
        return None

    slot_time_label = _format_slot_time(
        matched_slot_index,
        schedule.start_hour,
        schedule.check_interval_minutes,
    )
    logger.info(
        "Current JST time %s matched posting slot %s within the %s-minute grace window.",
        current_time.strftime("%H:%M"),
        slot_time_label,
        grace_minutes,
    )
    return PostingOpportunity(
        slot_index=matched_slot_index,
        slot_time_label=slot_time_label,
        grace_minutes=grace_minutes,
        minutes_since_slot=minutes_since_slot,
    )


def _get_gemini_model_names() -> list[str]:
    configured_models = os.environ.get("GEMINI_MODELS", "")
    if configured_models.strip():
        model_names = [name.strip() for name in configured_models.split(",") if name.strip()]
    elif os.environ.get("GEMINI_MODEL", "").strip():
        model_name = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
        model_names = [model_name] if model_name else []
    else:
        model_names = DEFAULT_GEMINI_MODELS[:]

    if not model_names:
        model_names = DEFAULT_GEMINI_MODELS[:]

    deduplicated = []
    seen = set()
    for model_name in model_names:
        if model_name not in seen:
            deduplicated.append(model_name)
            seen.add(model_name)

    return deduplicated


def _build_gemini_model(model_name: str):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing required environment variable: GEMINI_API_KEY")

    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)


def _sanitize_generated_text(text: str, preserve_line_breaks: bool = False) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"#[^\s#]+", "", cleaned)
    cleaned = cleaned.strip(" 　\"'")

    if preserve_line_breaks:
        lines = []
        previous_blank = False
        for raw_line in cleaned.split("\n"):
            line = re.sub(r"\s+", " ", raw_line).strip(" 　\"'")
            if not line:
                if not previous_blank:
                    lines.append("")
                previous_blank = True
                continue
            lines.append(line)
            previous_blank = False
        return "\n".join(lines).strip()

    cleaned = cleaned.replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _extract_json_block(text: str) -> str:
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced_match:
        return fenced_match.group(1)

    object_match = re.search(r"\{.*\}", text, re.DOTALL)
    if object_match:
        return object_match.group(0)

    return text


def _normalize_hashtag(tag: str) -> str:
    normalized = (tag or "").strip()
    normalized = normalized.replace("\n", " ")
    normalized = normalized.strip(" 　\"'")
    normalized = normalized.replace("＃", "#")
    normalized = normalized.replace(" ", "")
    if not normalized:
        return ""
    if not normalized.startswith("#"):
        normalized = f"#{normalized.lstrip('#')}"
    normalized = re.sub(r"[^\w#ぁ-んァ-ン一-龠ー]", "", normalized)
    return normalized if len(normalized) > 1 else ""


def _split_long_line(text: str, target_length: int = 28) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    lines = []
    remaining = stripped
    while len(remaining) > target_length:
        split_at = -1
        for marker in ("。", "！", "？", "!", "?", "、", ","):
            marker_index = remaining.rfind(marker, 0, target_length + 1)
            if marker_index >= 0:
                split_at = max(split_at, marker_index + 1)
        if split_at <= 0:
            split_at = target_length
        lines.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        lines.append(remaining)
    return lines


def _format_body_layout(body_text: str) -> str:
    sanitized = _sanitize_generated_text(body_text, preserve_line_breaks=True)
    if not sanitized:
        return ""

    candidate_lines = []
    for block in sanitized.splitlines():
        if not block.strip():
            continue
        candidate_lines.extend(_split_long_line(block))

    if not candidate_lines:
        return ""

    return "\n".join(candidate_lines[:3]).strip()


def _format_hashtag_block(hashtags: list[str]) -> str:
    if not hashtags:
        return ""

    first_line = " ".join(hashtags[:3])
    second_line = " ".join(hashtags[3:6])
    if second_line:
        return f"{first_line}\n{second_line}"
    return first_line


def _build_post_text(body_text: str, hashtags: list[str]) -> str:
    body = _format_body_layout(body_text)
    hashtag_block = _format_hashtag_block(hashtags)
    if body and hashtag_block:
        return f"{body}\n\n{hashtag_block}".strip()
    return body or hashtag_block


def _merge_hashtags_with_fallback(generated_hashtags: list[str]) -> list[str]:
    hashtags = []
    seen = set()

    for tag in generated_hashtags:
        if tag and tag not in seen:
            hashtags.append(tag)
            seen.add(tag)

    for tag in HASHTAG_HINTS:
        normalized = _normalize_hashtag(tag)
        if normalized and normalized not in seen:
            hashtags.append(normalized)
            seen.add(normalized)
        if len(hashtags) >= 6:
            break

    return hashtags[:6]


def _parse_generated_post(response_text: str) -> Optional[GeneratedPost]:
    raw_text = _extract_json_block(response_text.strip())

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Gemini response as JSON: %s", exc)
        logger.debug("Gemini raw response: %s", response_text)
        return None

    body = _sanitize_generated_text(str(payload.get("body", "")), preserve_line_breaks=True)
    raw_hashtags = payload.get("hashtags", [])
    if not isinstance(raw_hashtags, list):
        logger.error("Gemini response hashtags field is not a list.")
        return None

    generated_hashtags = []
    seen = set()
    for tag in raw_hashtags:
        normalized = _normalize_hashtag(str(tag))
        if not normalized or normalized in seen:
            continue
        generated_hashtags.append(normalized)
        seen.add(normalized)

    if not body:
        logger.error("Gemini response body is empty.")
        return None

    hashtags = _merge_hashtags_with_fallback(generated_hashtags)
    if len(generated_hashtags) < 5:
        logger.warning(
            "Gemini returned only %s usable hashtags. Filled the remainder with fallback hashtags.",
            len(generated_hashtags),
        )
    if not hashtags:
        logger.error("No usable hashtags were available after fallback.")
        return None

    return GeneratedPost(body=body, hashtags=hashtags[:6])


def generate_x_summary(article: Article) -> Optional[GeneratedPost]:
    hashtag_examples = ", ".join(HASHTAG_HINTS)
    prompt = f"""
あなたはX投稿のプロ編集者です。
以下の記事情報をもとに、日本語でX用の本文とハッシュタグを作成してください。

目的:
- 読者のやる気を引き出す
- 続きが気になり、フォローしたくなる空気を作る
- 押しつけがましくない

必須視点:
- 「凡人が仕組みで勝つ」
- 「教育のプロ」

出力ルール:
- 本文は70〜95文字を目安にする
- 本文は2〜3行で、1行ごとに短く読みやすくする
- 改行を使って、視線が止まりやすいレイアウトにする
- URLは含めない
- ハッシュタグは5〜6個作る
- ハッシュタグは検索されやすい一般的な語を優先する
- ハッシュタグは本文と関連し、短く自然なものにする
- 絵文字は使わない
- 余計な前置きや説明は不要
- 必ずJSONだけを返す
- JSON形式は次の通り:
  {{"body":"1行目\\n2行目\\n3行目","hashtags":["#タグ1","#タグ2","#タグ3","#タグ4","#タグ5"]}}

参考ハッシュタグ例:
{hashtag_examples}

記事タイトル:
{article.title}

記事本文:
{article.content[:3000]}
""".strip()

    model_names = _get_gemini_model_names()
    last_error = None

    for model_name in model_names:
        try:
            logger.info("Attempting Gemini generation with model: %s", model_name)
            model = _build_gemini_model(model_name)
            response = model.generate_content(prompt)
            text = getattr(response, "text", "") or ""
            text = text.strip()

            if not text:
                logger.warning("Gemini returned an empty response for model: %s", model_name)
                last_error = f"empty response from {model_name}"
                continue

            generated_post = _parse_generated_post(text)
            if not generated_post:
                logger.warning("Gemini response format was invalid for model: %s", model_name)
                last_error = f"invalid response format from {model_name}"
                continue

            logger.info("Generated X summary successfully with model: %s", model_name)
            return generated_post
        except Exception as exc:
            last_error = str(exc)
            logger.warning("Gemini model failed, trying next if available: %s", model_name)
            logger.exception("Failed to generate summary with Gemini model %s: %s", model_name, exc)

    logger.error("All configured Gemini models failed. last_error=%s", last_error)
    return None


def publish_with_hashtag_retry(body: str, hashtags: list[str], url: str) -> bool:
    normalized_hashtags = hashtags[:6]
    if len(normalized_hashtags) < 5:
        logger.error("Not enough hashtags to start posting.")
        return False

    for tag_count in range(len(normalized_hashtags), -1, -1):
        candidate_hashtags = normalized_hashtags[:tag_count]
        post_text = _build_post_text(body, candidate_hashtags)

        if len(post_text) > 140:
            logger.warning(
                "Post text is %s characters with %s hashtags. Reducing hashtags and retrying.",
                len(post_text),
                tag_count,
            )
            continue

        logger.info(
            "Attempting post with %s hashtags. text_length=%s",
            tag_count,
            len(post_text),
        )
        result = publish_to_x_detailed(
            text=post_text,
            url=url,
            apply_jitter=(tag_count == len(normalized_hashtags)),
        )
        if result.success:
            return True

        if result.error_type == "text_too_long":
            logger.warning(
                "X rejected the post as too long with %s hashtags. Reducing hashtags and retrying.",
                tag_count,
            )
            continue

        logger.error("Posting to X failed: %s", result.message or result.error_type)
        return False

    logger.error("Post error: still failed after removing all hashtags.")
    return False


def run() -> str:
    posting_opportunity = get_posting_opportunity()
    if not posting_opportunity:
        logger.info("RESULT: SKIPPED - no post is scheduled for the current JST slot.")
        return "skipped"

    duplicate_lookback_minutes = posting_opportunity.minutes_since_slot + posting_opportunity.grace_minutes
    if has_recent_feed_reply(lookback_minutes=duplicate_lookback_minutes):
        logger.info(
            "RESULT: SKIPPED - slot %s already appears to have been posted recently.",
            posting_opportunity.slot_time_label,
        )
        return "skipped"

    articles = fetch_articles()
    if not articles:
        logger.error("RESULT: ERROR - failed to fetch articles.")
        return "error"

    selection_mode = os.environ.get("ARTICLE_SELECTION_MODE", DEFAULT_SELECTION_MODE)
    article = choose_article(articles, selection_mode)
    if not article:
        logger.error("No article was selected for posting.")
        logger.error("RESULT: ERROR - no article was selected.")
        return "error"

    if was_url_recently_posted(article.url):
        logger.info("RESULT: SKIPPED - this article URL appears to have been posted recently: %s", article.url)
        return "skipped"

    generated_post = generate_x_summary(article)
    if not generated_post:
        logger.error("Summary generation failed. Posting aborted.")
        logger.error("RESULT: ERROR - failed to generate summary.")
        return "error"

    logger.info("Publishing generated summary to X. source=%s url=%s", article.source, article.url)
    result = publish_with_hashtag_retry(
        body=generated_post.body,
        hashtags=generated_post.hashtags,
        url=article.url,
    )
    if not result:
        logger.error("Posting to X failed.")
        logger.error("RESULT: ERROR - posting to X failed.")
        return "error"

    logger.info("Posting flow completed successfully.")
    logger.info("RESULT: POSTED")
    return "posted"


if __name__ == "__main__":
    raise SystemExit(0 if run() != "error" else 1)
