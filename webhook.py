import logging
import time
from datetime import datetime
import requests
from models import db, User, WebhookConfig, Favorite
from store_api import get_user_store

logger = logging.getLogger(__name__)


def send_webhook(url: str, payload: dict) -> bool:
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(f"Webhook 发送成功: {url}")
        return True
    except Exception as e:
        logger.error(f"Webhook 发送失败 ({url}): {e}")
        return False


def notify_daily_store(user: User, store_data: dict):
    webhooks = WebhookConfig.query.filter_by(is_active=True).all()
    if not webhooks and not user.webhook_url:
        return

    payload = {
        "event": "daily_store",
        "user": user.display_name or user.login_name,
        "region": user.region,
        "timestamp": datetime.now().isoformat(),
        "offers": store_data["offers"],
        "favorites_matched": store_data.get("favorites_matched", []),
    }

    if user.webhook_url:
        send_webhook(user.webhook_url, payload)

    for wh in webhooks:
        send_webhook(wh.url, payload)


def process_all_users():
    logger.info("开始检查所有用户的每日商店...")
    users = User.query.all()
    success_count = 0
    error_count = 0

    for user in users:
        if not user.riot_bound:
            logger.info(f"跳过未绑定用户: {user.display_name or user.login_name}")
            continue
        logger.info(f"正在检查用户: {user.display_name or user.login_name}")
        store_data = get_user_store(user)

        if store_data.get("error"):
            logger.error(f"用户 {user.login_name} 获取商店失败: {store_data['error']}")
            error_count += 1
        else:
            notify_daily_store(user, store_data)
            success_count += 1

        if user != users[-1]:
            time.sleep(2)

    logger.info(f"每日商店检查完成: {success_count} 成功, {error_count} 失败")
    return {"success": success_count, "error": error_count}
