import time as _time
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional, Tuple

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from config import get_fernet

db = SQLAlchemy()
URL_TOKEN_PREFIX = "__URL_TOKEN__"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    login_name = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    display_name = db.Column(db.String(100), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)

    # Riot 绑定（注册后绑定，可为空）
    riot_username = db.Column(db.String(100), nullable=True)
    encrypted_riot_password = db.Column(db.LargeBinary, nullable=True)
    region = db.Column(db.String(10), nullable=True)
    puuid = db.Column(db.String(100), nullable=True)

    webhook_url = db.Column(db.String(500), nullable=True)
    notification_language = db.Column(db.String(10), nullable=False, default="zh")
    notification_timezone = db.Column(
        db.String(64), nullable=False, default="Asia/Shanghai"
    )
    notification_timezone_auto = db.Column(db.Boolean, nullable=False, default=True)
    notification_reminder_time = db.Column(
        db.String(5), nullable=False, default="08:00"
    )
    last_rebind_reminder_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    favorites = db.relationship(
        "Favorite", backref="user", cascade="all, delete-orphan"
    )
    store_offers = db.relationship(
        "StoreOffer", backref="user", cascade="all, delete-orphan"
    )
    accessory_offers = db.relationship(
        "AccessoryOffer", backref="user", cascade="all, delete-orphan"
    )

    def set_login_password(self, plaintext: str):
        self.password_hash = generate_password_hash(plaintext)

    def check_login_password(self, plaintext: str) -> bool:
        return check_password_hash(self.password_hash, plaintext)

    def set_riot_password(self, plaintext: str):
        fernet = get_fernet()
        self.encrypted_riot_password = fernet.encrypt(plaintext.encode())

    def get_riot_password(self) -> str:
        if not self.encrypted_riot_password:
            return ""
        fernet = get_fernet()
        return fernet.decrypt(self.encrypted_riot_password).decode()

    def set_url_access_token(self, access_token: str):
        """
        保存 URL 绑定得到的短期 access token。
        格式: __URL_TOKEN__:<issued_ts>:<access_token>
        """
        issued_ts = int(_time.time())
        payload = f"{URL_TOKEN_PREFIX}:{issued_ts}:{access_token}"
        self.set_riot_password(payload)

    def get_url_access_token(self) -> Tuple[Optional[str], Optional[int]]:
        raw = self.get_riot_password()
        prefix = f"{URL_TOKEN_PREFIX}:"
        if not raw.startswith(prefix):
            return None, None
        try:
            _, issued_ts_str, token = raw.split(":", 2)
            return token, int(issued_ts_str)
        except Exception:
            return None, None

    @property
    def riot_bound(self) -> bool:
        # URL 绑定：有 PUUID 和区服即视为已绑定
        return bool(self.puuid and self.region)

    @property
    def shard(self) -> str:
        from config import REGION_TO_SHARD

        return REGION_TO_SHARD.get(self.region, "na")


class Skin(db.Model):
    __tablename__ = "skins"

    uuid: Mapped[str] = mapped_column(db.String(36), primary_key=True)
    name: Mapped[str] = mapped_column(db.String(200))
    name_i18n: Mapped[str | None] = mapped_column(db.Text, default=None)
    icon_url: Mapped[str | None] = mapped_column(db.String(500), default=None)
    tier_name: Mapped[str | None] = mapped_column(db.String(50), default=None)
    tier_icon: Mapped[str | None] = mapped_column(db.String(500), default=None)
    cost: Mapped[int | None] = mapped_column(db.Integer, default=None)
    weapon_name: Mapped[str | None] = mapped_column(db.String(100), default=None)
    is_melee: Mapped[bool] = mapped_column(db.Boolean, default=False)
    updated_at: Mapped[datetime | None] = mapped_column(db.DateTime, default=None)

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            uuid: str,
            name: str,
            name_i18n: str | None = None,
            icon_url: str | None = None,
            tier_name: str | None = None,
            tier_icon: str | None = None,
            cost: int | None = None,
            weapon_name: str | None = None,
            is_melee: bool = False,
            updated_at: datetime | None = None,
        ) -> None: ...


class Favorite(db.Model):
    __tablename__ = "favorites"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    skin_uuid = db.Column(db.String(36), db.ForeignKey("skins.uuid"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    skin = db.relationship("Skin")

    __table_args__ = (db.UniqueConstraint("user_id", "skin_uuid"),)


class StoreOffer(db.Model):
    __tablename__ = "store_offers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    skin_uuid = db.Column(db.String(36), db.ForeignKey("skins.uuid"), nullable=False)
    offer_date = db.Column(db.Date, nullable=False, default=date.today)
    cost = db.Column(db.Integer, nullable=True)

    skin = db.relationship("Skin")

    __table_args__ = (db.UniqueConstraint("user_id", "skin_uuid", "offer_date"),)


class AccessoryOffer(db.Model):
    """当前每周配件商店快照。

    配件可能是挂饰、玩家卡、喷漆或称号，不能关联到 Skin 表，因此在
    抓取时保存展示所需的最小元数据。
    """

    __tablename__ = "accessory_offers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    offer_id = db.Column(db.String(36), nullable=False)
    item_uuid = db.Column(db.String(36), nullable=False)
    item_type_uuid = db.Column(db.String(36), nullable=False)
    item_type = db.Column(db.String(20), nullable=False, default="unknown")
    name = db.Column(db.String(200), nullable=False, default="未知配件")
    icon_url = db.Column(db.String(500), nullable=True)
    cost = db.Column(db.Integer, nullable=True)
    currency_uuid = db.Column(db.String(36), nullable=True)
    storefront_id = db.Column(db.String(100), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "offer_id", name="uq_accessory_user_offer"),
    )


class WebhookConfig(db.Model):
    __tablename__ = "webhook_configs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
