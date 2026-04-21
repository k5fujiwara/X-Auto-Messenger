import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Optional

import tweepy

from env_loader import load_env_file


load_env_file()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class PublishResult:
    success: bool
    error_type: Optional[str] = None
    message: Optional[str] = None
    parent_tweet_id: Optional[str] = None
    reply_tweet_id: Optional[str] = None


def _build_x_client() -> tweepy.Client:
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_secret = os.environ.get("X_ACCESS_SECRET")

    missing_keys = [
        name
        for name, value in (
            ("X_API_KEY", api_key),
            ("X_API_SECRET", api_secret),
            ("X_ACCESS_TOKEN", access_token),
            ("X_ACCESS_SECRET", access_secret),
        )
        if not value
    ]
    if missing_keys:
        raise ValueError(f"Missing required environment variables: {', '.join(missing_keys)}")

    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )


def _is_text_too_long_error(message: str) -> bool:
    normalized = (message or "").lower()
    return any(
        phrase in normalized
        for phrase in (
            "too long",
            "too many characters",
            "tweet needs to be a bit shorter",
            "post needs to be a bit shorter",
            "over 280",
            "character count",
            "text is too long",
        )
    )


def publish_to_x_detailed(text: str, url: str, apply_jitter: bool = True) -> PublishResult:
    if not text or not text.strip():
        logger.error("Parent post text is empty.")
        return PublishResult(success=False, error_type="empty_text", message="Parent post text is empty.")

    if not url or not url.strip():
        logger.error("Reply URL is empty.")
        return PublishResult(success=False, error_type="empty_url", message="Reply URL is empty.")

    if apply_jitter:
        wait_seconds = random.randint(0, 900)
        logger.info("Sleeping %s seconds before posting to X.", wait_seconds)
        time.sleep(wait_seconds)

    try:
        client = _build_x_client()

        logger.info("Posting parent tweet to X.")
        parent_response = client.create_tweet(text=text.strip())
        parent_data = parent_response.data or {}
        parent_tweet_id = parent_data.get("id")

        if not parent_tweet_id:
            logger.error("Parent tweet was not created successfully: %s", parent_response)
            return PublishResult(
                success=False,
                error_type="parent_post_failed",
                message=str(parent_response),
            )

        logger.info("Parent tweet posted successfully. tweet_id=%s", parent_tweet_id)

        logger.info("Posting reply tweet with URL.")
        reply_response = client.create_tweet(
            text=url.strip(),
            in_reply_to_tweet_id=parent_tweet_id,
        )
        reply_data = reply_response.data or {}
        reply_tweet_id = reply_data.get("id")

        if not reply_tweet_id:
            logger.error("Reply tweet was not created successfully: %s", reply_response)
            return PublishResult(
                success=False,
                error_type="reply_post_failed",
                message=str(reply_response),
                parent_tweet_id=parent_tweet_id,
            )

        logger.info(
            "Reply tweet posted successfully. parent_tweet_id=%s reply_tweet_id=%s",
            parent_tweet_id,
            reply_tweet_id,
        )
        return PublishResult(
            success=True,
            parent_tweet_id=parent_tweet_id,
            reply_tweet_id=reply_tweet_id,
        )
    except tweepy.TweepyException as exc:
        message = str(exc)
        error_type = "text_too_long" if _is_text_too_long_error(message) else "tweepy_error"
        logger.exception("Failed to publish to X via Tweepy: %s", exc)
        return PublishResult(success=False, error_type=error_type, message=message)
    except Exception as exc:
        logger.exception("Unexpected error while publishing to X: %s", exc)
        return PublishResult(success=False, error_type="unexpected_error", message=str(exc))


def publish_to_x(text: str, url: str) -> bool:
    result = publish_to_x_detailed(text=text, url=url, apply_jitter=True)
    return result.success
