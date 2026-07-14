import os
import re
import tempfile
from unittest.mock import patch

os.environ.setdefault("ALLOW_DEFAULT_ADMIN", "true")
os.environ.setdefault("FERNET_KEY", "f5pFG3aY9bvtPgkkkpia7LljMvISUG3qLLGQasP-A2g=")

from config import Config
from config import REGION_TIMEZONES
from app import create_app
from models import Skin, User, db
from security import validate_webhook_url


def _csrf_token(html: bytes) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html) or re.search(
        rb'name="csrf-token" content="([^"]+)"', html
    )
    assert match, "CSRF token missing"
    return match.group(1).decode()


def main() -> None:
    assert REGION_TIMEZONES["kr"] == "Asia/Seoul"
    assert REGION_TIMEZONES["br"] == "America/Sao_Paulo"
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file.close()

    Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_file.name}"
    app = create_app()
    app.config.update(
        TESTING=True,
    )

    with app.app_context():
        db.drop_all()
        db.create_all()
        user = User()
        user.login_name = "admin"
        user.display_name = "Admin"
        user.is_admin = True
        user.puuid = "test-puuid"
        user.region = "ap"
        user.set_login_password("admin123")
        db.session.add(user)
        db.session.add(Skin(uuid="00000000-0000-0000-0000-000000000001", name="Test Skin"))
        db.session.commit()

    client = app.test_client()

    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert login_page.headers["X-Frame-Options"] == "DENY"
    assert login_page.headers["X-Content-Type-Options"] == "nosniff"
    token = _csrf_token(login_page.data)

    missing_csrf = client.post(
        "/login", data={"login_name": "admin", "password": "admin123"}
    )
    assert missing_csrf.status_code == 400

    blocked_redirect = client.post(
        "/login?next=https://evil.example/",
        data={"login_name": "admin", "password": "admin123", "csrf_token": token},
        follow_redirects=False,
    )
    assert blocked_redirect.status_code == 302
    assert blocked_redirect.headers["Location"].endswith("/dashboard")

    dashboard = client.get("/dashboard")
    token = _csrf_token(dashboard.data)

    bad_favorite = client.post(
        "/my/favorites/toggle",
        json={"skin_uuid": "missing"},
        headers={"X-CSRFToken": token},
    )
    assert bad_favorite.status_code == 404

    add_favorite = client.post(
        "/my/favorites/toggle",
        json={"skin_uuid": "00000000-0000-0000-0000-000000000001"},
        headers={"X-CSRFToken": token},
    )
    assert add_favorite.status_code == 200
    assert add_favorite.get_json()["status"] == "added"

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    token = _csrf_token(settings_page.data)
    save_settings = client.post(
        "/settings",
        data={
            "notification_language": "en",
            "notification_timezone": "America/Los_Angeles",
            "notification_reminder_time": "09:30",
            "notification_timezone_auto": "1",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert save_settings.status_code == 302
    with app.app_context():
        saved_user = User.query.filter_by(login_name="admin").one()
        assert saved_user.notification_language == "en"
        assert saved_user.notification_timezone == "Asia/Shanghai"
        assert saved_user.notification_timezone_auto is True
        assert saved_user.notification_reminder_time == "09:30"

    settings_page = client.get("/settings")
    token = _csrf_token(settings_page.data)
    manual_timezone = client.post(
        "/settings",
        data={
            "notification_language": "en",
            "notification_timezone": "America/Los_Angeles",
            "notification_reminder_time": "09:30",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert manual_timezone.status_code == 302
    with app.app_context():
        saved_user = User.query.filter_by(login_name="admin").one()
        assert saved_user.notification_timezone == "America/Los_Angeles"
        assert saved_user.notification_timezone_auto is False

    favorites_page = client.get("/my/favorites")
    assert favorites_page.status_code == 200
    assert b"/my/webhook" in favorites_page.data
    token = _csrf_token(favorites_page.data)
    with patch("app.validate_webhook_url", return_value=None):
        save_webhook = client.post(
            "/my/webhook",
            data={
                "webhook_url": "https://hooks.example/user",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
    assert save_webhook.status_code == 302
    with app.app_context():
        saved_user = User.query.filter_by(login_name="admin").one()
        assert saved_user.webhook_url == "https://hooks.example/user"

    favorites_page = client.get("/my/favorites")
    token = _csrf_token(favorites_page.data)
    with patch("webhook.send_webhook", return_value=True) as send:
        test_webhook = client.post(
            "/my/webhook/test",
            data={"csrf_token": token},
            follow_redirects=False,
        )
    assert test_webhook.status_code == 302
    send.assert_called_once()

    dashboard = client.get("/dashboard")
    token = _csrf_token(dashboard.data)
    store_data = {
        "error": None,
        "offers": [],
        "favorites_matched": [],
        "accessory_offers": [],
    }
    with (
        patch("app.get_user_store", return_value=store_data),
        patch(
            "webhook.notify_daily_store",
            return_value={"attempted": 1, "success": 1, "failed": 0},
        ) as notify_store,
    ):
        refresh = client.post(
            "/my/store/refresh",
            data={"csrf_token": token},
            follow_redirects=False,
        )
    assert refresh.status_code == 302
    notify_store.assert_called_once()

    blocked_webhook = False
    try:
        validate_webhook_url("http://127.0.0.1:5000/hook")
    except ValueError:
        blocked_webhook = True
    assert blocked_webhook

    # A public hostname can resolve to a private proxy address in local
    # development. It is safe when the outbound request itself uses that
    # proxy, but must remain blocked for direct connections.
    proxy_address = [
        (2, 1, 6, "", ("172.19.0.10", 0)),
    ]
    with (
        patch("security.socket.getaddrinfo", return_value=proxy_address),
        patch("security._request_uses_proxy", return_value=True),
    ):
        validate_webhook_url("https://discord.com/api/webhooks/example/token")

    blocked_proxy_target = False
    with (
        patch("security.socket.getaddrinfo", return_value=proxy_address),
        patch("security._request_uses_proxy", return_value=False),
    ):
        try:
            validate_webhook_url("https://discord.com/api/webhooks/example/token")
        except ValueError:
            blocked_proxy_target = True
    assert blocked_proxy_target

    non_discord_webhook = False
    try:
        validate_webhook_url("https://hooks.slack.com/services/example")
    except ValueError:
        non_discord_webhook = True
    assert non_discord_webhook

    blocked_scheme = False
    try:
        validate_webhook_url("file:///etc/passwd")
    except ValueError:
        blocked_scheme = True
    assert blocked_scheme

    print("security smoke tests passed")


if __name__ == "__main__":
    main()
