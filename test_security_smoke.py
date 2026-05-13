import os
import re
import tempfile

os.environ.setdefault("ALLOW_DEFAULT_ADMIN", "true")
os.environ.setdefault("FERNET_KEY", "f5pFG3aY9bvtPgkkkpia7LljMvISUG3qLLGQasP-A2g=")

from config import Config
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

    blocked_webhook = False
    try:
        validate_webhook_url("http://127.0.0.1:5000/hook")
    except ValueError:
        blocked_webhook = True
    assert blocked_webhook

    blocked_scheme = False
    try:
        validate_webhook_url("file:///etc/passwd")
    except ValueError:
        blocked_scheme = True
    assert blocked_scheme

    print("security smoke tests passed")


if __name__ == "__main__":
    main()
