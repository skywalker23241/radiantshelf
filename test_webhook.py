from unittest.mock import patch
from datetime import datetime, timezone

from flask import Flask

from i18n import load_translations
from models import User, WebhookConfig, db
from webhook import (
    _payload_for_endpoint,
    build_error_payload,
    build_store_payload,
    notify_daily_store,
    process_all_users,
    send_rebind_reminders,
)


def _user(login_name: str, *, bound: bool = True) -> User:
    user = User()
    user.login_name = login_name
    user.display_name = login_name.title()
    user.set_login_password("test-password")
    if bound:
        user.puuid = f"{login_name}-puuid"
        user.region = "ap"
    return user


def main() -> None:
    load_translations()
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)

    with app.app_context():
        db.create_all()
        user = _user("notify")
        user.webhook_url = "https://hooks.example/user"
        user.notification_timezone = "UTC"
        user.notification_reminder_time = "08:00"
        unbound = _user("unbound", bound=False)

        duplicate = WebhookConfig()
        duplicate.name = "duplicate"
        duplicate.url = user.webhook_url
        duplicate.is_active = True
        global_hook = WebhookConfig()
        global_hook.name = "global"
        global_hook.url = "https://hooks.example/global"
        global_hook.is_active = True
        inactive = WebhookConfig()
        inactive.name = "inactive"
        inactive.url = "https://hooks.example/inactive"
        inactive.is_active = False
        db.session.add_all([user, unbound, duplicate, global_hook, inactive])
        db.session.commit()

        favorite_offer = {
            "uuid": "favorite-skin",
            "name": "Favorite Skin",
            "icon_url": "https://example.test/skin.png",
            "cost": 1775,
        }
        store_data = {
            "offers": [favorite_offer],
            "favorites_matched": [favorite_offer],
            "remaining_seconds": 3600,
            "accessory_offers": [{"uuid": "spray", "name": "Test Spray"}],
            "accessory_remaining_seconds": 7200,
        }

        payload = build_store_payload(user, store_data)
        assert payload["event"] == "daily_store"
        assert payload["has_favorite_match"] is True
        assert payload["favorite_match_count"] == 1
        assert payload["favorites_matched"][0]["uuid"] == "favorite-skin"
        assert payload["accessory_offers"][0]["uuid"] == "spray"

        discord_payload = _payload_for_endpoint(
            "https://discord.com/api/webhooks/example/token", payload, "en"
        )
        assert discord_payload["content"].startswith("**RadiantShelf | Daily Store**")
        assert "Favorite Skin" in discord_payload["content"]
        assert discord_payload["allowed_mentions"] == {"parse": []}
        assert _payload_for_endpoint("https://hooks.example/user", payload) is payload

        reminder_payload = _payload_for_endpoint(
            "https://discord.com/api/webhooks/example/token",
            {"event": "rebind_reminder", "minutes": 15},
            "en",
        )
        assert "rebind" in reminder_payload["content"].lower()

        with patch("webhook.send_webhook", side_effect=[True, False]) as send:
            delivery = notify_daily_store(user, store_data)
        assert send.call_count == 2
        assert delivery == {"attempted": 2, "success": 1, "failed": 1}

        with patch("webhook.send_webhook", return_value=True) as send:
            reminders = send_rebind_reminders(
                datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
            )
        send.assert_called_once()
        assert reminders == {"attempted": 1, "success": 1, "failed": 0, "skipped": 0}

        expired = build_error_payload(user, "登录凭据已过期，请重新绑定")
        assert expired["event"] == "store_check_failed"
        assert expired["action_required"] == "rebind"

        with (
            patch("webhook.get_user_store", return_value=store_data),
            patch(
                "webhook.notify_daily_store",
                return_value={"attempted": 2, "success": 2, "failed": 0},
            ),
        ):
            batch = process_all_users()
        assert batch == {
            "success": 1,
            "error": 0,
            "skipped": 1,
            "notifications_sent": 2,
            "notification_errors": 0,
            "favorite_matches": 1,
        }

        with (
            patch("webhook.get_user_store", side_effect=RuntimeError("boom")),
            patch(
                "webhook.notify_store_error",
                return_value={"attempted": 1, "success": 1, "failed": 0},
            ) as notify_error,
        ):
            failed_batch = process_all_users()
        assert failed_batch["success"] == 0
        assert failed_batch["error"] == 1
        assert failed_batch["notifications_sent"] == 1
        notify_error.assert_called_once()

    print("webhook and favorite-match tests passed")


if __name__ == "__main__":
    main()
