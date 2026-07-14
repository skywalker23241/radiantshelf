import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from i18n import DEFAULT_LANG, SUPPORTED_LANGS, localized_skin_name_for_locale, translate_for_locale
from models import Skin, User, WebhookConfig, db
from security import validate_webhook_url
from store_api import get_user_store

logger = logging.getLogger(__name__)


def _is_discord_webhook(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    is_discord_host = hostname in {"discord.com", "discordapp.com"} or hostname.endswith(
        ".discord.com"
    ) or hostname.endswith(".discordapp.com")
    return is_discord_host and parsed.path.startswith("/api/webhooks/")


def _localized_offer_names(offers: List[Dict[str, Any]], language: str) -> List[str]:
    names = []
    for offer in offers:
        skin_uuid = offer.get("uuid")
        skin = db.session.get(Skin, skin_uuid) if isinstance(skin_uuid, str) else None
        names.append(
            localized_skin_name_for_locale(skin, language)
            if skin is not None
            else str(offer.get("name", "Unknown skin"))
        )
    return names


def _discord_payload(payload: Dict[str, Any], language: str = DEFAULT_LANG) -> Dict[str, Any]:
    """Convert the app's generic event payload into a Discord message."""
    event = payload.get("event")
    if event == "daily_store":
        offers = payload.get("offers") or []
        offer_names = _localized_offer_names(
            [offer for offer in offers if isinstance(offer, dict)], language
        )
        lines = [
            f"**{translate_for_locale(language, 'discord_daily_title')}**",
            f"{translate_for_locale(language, 'discord_player')}: {payload.get('user') or '-'}",
            f"{translate_for_locale(language, 'discord_region')}: {payload.get('region') or '-'}",
        ]
        if offer_names:
            lines.append(
                f"{translate_for_locale(language, 'discord_today_skins')}: "
                + ", ".join(offer_names)
            )
        match_count = payload.get("favorite_match_count", 0)
        if match_count:
            lines.append(
                f"⭐ {translate_for_locale(language, 'discord_favorite_matches')}: {match_count}"
            )
        content = "\n".join(lines)
    elif event == "store_check_failed":
        content = "\n".join(
            [
                f"**{translate_for_locale(language, 'discord_store_error_title')}**",
                f"{translate_for_locale(language, 'discord_player')}: {payload.get('user') or '-'}",
                f"{translate_for_locale(language, 'discord_reason')}: {payload.get('error') or '-'}",
            ]
        )
    elif event == "rebind_reminder":
        content = "\n".join(
            [
                f"**{translate_for_locale(language, 'discord_rebind_reminder_title')}**",
                translate_for_locale(
                    language,
                    "discord_rebind_reminder_body",
                ),
            ]
        )
    else:
        content = str(
            payload.get("message")
            or translate_for_locale(language, "discord_test_message")
        )

    # Avoid interpreting skin or user names as @mentions in Discord.
    return {"content": content[:2000], "allowed_mentions": {"parse": []}}


def _payload_for_endpoint(
    url: str, payload: Dict[str, Any], language: str = DEFAULT_LANG
) -> Dict[str, Any]:
    return _discord_payload(payload, language) if _is_discord_webhook(url) else payload


def send_webhook(url: str, payload: dict, language: str = DEFAULT_LANG) -> bool:
    """Send a JSON webhook and report whether the endpoint accepted it."""
    try:
        validate_webhook_url(url)
        response = requests.post(
            url, json=_payload_for_endpoint(url, payload, language), timeout=10
        )
        response.raise_for_status()
        logger.info("Webhook 发送成功: %s", url)
        return True
    except Exception as exc:
        logger.error("Webhook 发送失败 (%s): %s", url, exc)
        return False


def build_store_payload(user: User, store_data: Dict[str, Any]) -> Dict[str, Any]:
    """Build the public payload used by both manual and scheduled checks."""
    matches = store_data.get("favorites_matched") or []
    return {
        "event": "daily_store",
        "user": user.display_name or user.login_name,
        "region": user.region,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "offers": store_data.get("offers") or [],
        "remaining_seconds": store_data.get("remaining_seconds", 0),
        "accessory_offers": store_data.get("accessory_offers") or [],
        "accessory_remaining_seconds": store_data.get(
            "accessory_remaining_seconds", 0
        ),
        "has_favorite_match": bool(matches),
        "favorite_match_count": len(matches),
        "favorites_matched": matches,
    }


def build_error_payload(user: User, error: str) -> Dict[str, Any]:
    return {
        "event": "store_check_failed",
        "user": user.display_name or user.login_name,
        "region": user.region,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "action_required": "rebind" if "过期" in error or "失效" in error else None,
    }


def _notification_targets(user: User) -> List[Tuple[str, str]]:
    """Return user and global webhook targets without duplicate URLs."""
    candidates: List[Tuple[str, str]] = []
    if user.webhook_url:
        candidates.append(("user", user.webhook_url.strip()))
    for webhook in WebhookConfig.query.filter_by(is_active=True).all():
        candidates.append((webhook.name, webhook.url.strip()))

    targets: List[Tuple[str, str]] = []
    seen = set()
    for name, url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        targets.append((name, url))
    return targets


def _deliver(user: User, payload: Dict[str, Any]) -> Dict[str, int]:
    result = {"attempted": 0, "success": 0, "failed": 0}
    language = user.notification_language
    if language not in SUPPORTED_LANGS:
        language = DEFAULT_LANG
    for target_name, url in _notification_targets(user):
        result["attempted"] += 1
        if send_webhook(url, payload, language):
            result["success"] += 1
        else:
            result["failed"] += 1
            logger.warning(
                "用户 %s 的 Webhook 端点 %s 推送失败",
                user.login_name,
                target_name,
            )
    return result


def _deliver_personal(user: User, payload: Dict[str, Any]) -> Dict[str, int]:
    """Deliver privacy-sensitive account reminders only to a user's channel."""
    if not user.webhook_url:
        return {"attempted": 0, "success": 0, "failed": 0}

    language = user.notification_language
    if language not in SUPPORTED_LANGS:
        language = DEFAULT_LANG
    ok = send_webhook(user.webhook_url.strip(), payload, language)
    return {"attempted": 1, "success": int(ok), "failed": int(not ok)}


def notify_daily_store(user: User, store_data: Dict[str, Any]) -> Dict[str, int]:
    return _deliver(user, build_store_payload(user, store_data))


def notify_store_error(user: User, error: str) -> Dict[str, int]:
    return _deliver(user, build_error_payload(user, error))


def send_rebind_reminders(now: datetime | None = None) -> Dict[str, int]:
    """Send a once-daily rebind reminder at each user's local preferred time."""
    result = {"attempted": 0, "success": 0, "failed": 0, "skipped": 0}
    token_max_age = 55 * 60
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    has_updates = False
    for user in User.query.filter(User.webhook_url.isnot(None)).all():
        if not user.riot_bound:
            result["skipped"] += 1
            continue
        try:
            user_timezone = ZoneInfo(user.notification_timezone)
        except ZoneInfoNotFoundError:
            user_timezone = ZoneInfo("Asia/Shanghai")
        local_now = now.astimezone(user_timezone)
        try:
            reminder_hour, reminder_minute = map(
                int, user.notification_reminder_time.split(":", 1)
            )
        except (AttributeError, ValueError):
            reminder_hour, reminder_minute = 8, 0
        if (local_now.hour, local_now.minute) != (reminder_hour, reminder_minute):
            result["skipped"] += 1
            continue
        if user.last_rebind_reminder_date == local_now.date():
            result["skipped"] += 1
            continue
        token, issued_at = user.get_url_access_token()
        token_age = time.time() - issued_at if issued_at is not None else token_max_age
        if not token or token_age >= token_max_age:
            delivery = _deliver_personal(
                user,
                {"event": "rebind_reminder"},
            )
            result["attempted"] += delivery["attempted"]
            result["success"] += delivery["success"]
            result["failed"] += delivery["failed"]
            user.last_rebind_reminder_date = local_now.date()
            has_updates = True
        else:
            result["skipped"] += 1
    if has_updates:
        db.session.commit()
    return result


def process_all_users() -> Dict[str, int]:
    logger.info("开始检查所有用户的每日商店...")
    users = User.query.all()
    result = {
        "success": 0,
        "error": 0,
        "skipped": 0,
        "notifications_sent": 0,
        "notification_errors": 0,
        "favorite_matches": 0,
    }

    bound_users = [user for user in users if user.riot_bound]
    result["skipped"] = len(users) - len(bound_users)
    for index, user in enumerate(bound_users):
        logger.info("正在检查用户: %s", user.display_name or user.login_name)
        try:
            store_data = get_user_store(user)
        except Exception as exc:
            logger.exception("用户 %s 商店检查发生异常", user.login_name)
            store_data = {"error": f"商店检查异常: {exc}"}

        if store_data.get("error"):
            error = str(store_data["error"])
            logger.error("用户 %s 获取商店失败: %s", user.login_name, error)
            delivery = notify_store_error(user, error)
            result["error"] += 1
        else:
            delivery = notify_daily_store(user, store_data)
            result["success"] += 1
            result["favorite_matches"] += len(
                store_data.get("favorites_matched") or []
            )

        result["notifications_sent"] += delivery["success"]
        result["notification_errors"] += delivery["failed"]
        if index < len(bound_users) - 1:
            time.sleep(2)

    logger.info(
        "每日商店检查完成: %s 成功, %s 失败, %s 条推送成功, %s 条推送失败",
        result["success"],
        result["error"],
        result["notifications_sent"],
        result["notification_errors"],
    )
    return result
