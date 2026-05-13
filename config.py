import os
from cryptography.fernet import Fernet

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(BASE_DIR, 'store.db')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = (
        os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    )
    FERNET_KEY = os.environ.get("FERNET_KEY", "")
    CHECK_HOUR = int(os.environ.get("CHECK_HOUR", "8"))
    CHECK_MINUTE = int(os.environ.get("CHECK_MINUTE", "0"))
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Shanghai")
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
    ALLOW_DEFAULT_ADMIN = (
        os.environ.get("ALLOW_DEFAULT_ADMIN", "false").lower() == "true"
    )
    ALLOW_PRIVATE_WEBHOOKS = (
        os.environ.get("ALLOW_PRIVATE_WEBHOOKS", "false").lower() == "true"
    )


REGION_TO_SHARD = {
    "na": "na",
    "br": "na",
    "latam": "na",
    "pbe": "na",
    "eu": "eu",
    "tr": "eu",
    "ru": "eu",
    "ap": "ap",
    "kr": "ap",
}

REGIONS = [
    ("na", "北美 (NA)"),
    ("eu", "欧洲 (EU)"),
    ("ap", "亚太 (AP)"),
    ("kr", "韩国 (KR)"),
    ("br", "巴西 (BR)"),
    ("latam", "拉美 (LATAM)"),
    ("tr", "土耳其 (TR)"),
    ("ru", "俄罗斯 (RU)"),
]

_fernet_instance = None


def get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    key = Config.FERNET_KEY
    if not key:
        key_file = os.path.join(BASE_DIR, "fernet.key")
        if os.path.exists(key_file):
            with open(key_file, "rb") as f:
                key = f.read().strip()
        else:
            key = Fernet.generate_key()
            with open(key_file, "wb") as f:
                f.write(key)
    elif isinstance(key, str):
        key = key.encode()

    _fernet_instance = Fernet(key)
    return _fernet_instance
