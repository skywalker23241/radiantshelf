import logging
from datetime import date, datetime
from functools import wraps
from typing import Optional, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

from config import REGION_TIMEZONES, REGIONS, Config
from accessory_cache import get_accessory_metadata
from i18n import SUPPORTED_LANGS, init_i18n
from i18n import translate as _
from models import AccessoryOffer, Favorite, Skin, StoreOffer, User, WebhookConfig, db
from riot_auth import AuthenticationError, RiotAuth
from security import (
    get_csrf_token,
    is_safe_redirect_url,
    validate_csrf_token,
    validate_webhook_url,
)
from skin_cache import is_cache_stale, refresh_skin_cache, search_skins
from store_api import detect_shard_by_token, get_user_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

login_manager = LoginManager()
# 某些类型桩未声明这些动态属性，使用 setattr 可避免 IDE 误报
setattr(login_manager, "login_view", "login")
setattr(login_manager, "login_message", "请先登录")
setattr(login_manager, "login_message_category", "warning")


def _current_user() -> User:
    return cast(User, current_user)


def _current_user_id() -> int:
    uid = _current_user().id
    if uid is None:
        raise RuntimeError("当前用户ID为空")
    return int(uid)


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not _current_user().is_admin:
            flash("需要管理员权限", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)

    return decorated


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    init_i18n(app)

    @login_manager.user_loader
    def load_user(user_id: str) -> Optional[User]:
        return db.session.get(User, int(user_id))

    @app.before_request
    def protect_state_changing_requests():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            validate_csrf_token()

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        return response

    @app.context_processor
    def inject_security_helpers():
        return {"csrf_token": get_csrf_token}

    with app.app_context():
        db.create_all()

        # 迁移: 为已有 skins 表添加 name_i18n 列
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text

        inspector = sa_inspect(db.engine)
        skin_cols = [c["name"] for c in inspector.get_columns("skins")]
        user_cols = [c["name"] for c in inspector.get_columns("users")]
        if "name_i18n" not in skin_cols:
            db.session.execute(text("ALTER TABLE skins ADD COLUMN name_i18n TEXT"))
            db.session.commit()
        if "is_melee" not in skin_cols:
            db.session.execute(
                text("ALTER TABLE skins ADD COLUMN is_melee BOOLEAN DEFAULT 0")
            )
            db.session.commit()
        if "notification_language" not in user_cols:
            db.session.execute(
                text(
                    "ALTER TABLE users ADD COLUMN notification_language "
                    "VARCHAR(10) NOT NULL DEFAULT 'zh'"
                )
            )
            db.session.commit()
        if "notification_timezone" not in user_cols:
            db.session.execute(
                text(
                    "ALTER TABLE users ADD COLUMN notification_timezone "
                    "VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai'"
                )
            )
            db.session.commit()
        if "notification_timezone_auto" not in user_cols:
            db.session.execute(
                text(
                    "ALTER TABLE users ADD COLUMN notification_timezone_auto "
                    "BOOLEAN NOT NULL DEFAULT 1"
                )
            )
            db.session.commit()
        if "notification_reminder_time" not in user_cols:
            db.session.execute(
                text(
                    "ALTER TABLE users ADD COLUMN notification_reminder_time "
                    "VARCHAR(5) NOT NULL DEFAULT '08:00'"
                )
            )
            db.session.commit()
        if "last_rebind_reminder_date" not in user_cols:
            db.session.execute(
                text("ALTER TABLE users ADD COLUMN last_rebind_reminder_date DATE")
            )
            db.session.commit()

        if not User.query.filter_by(is_admin=True).first():
            admin = User()
            if Config.ADMIN_USERNAME and Config.ADMIN_PASSWORD:
                admin.login_name = Config.ADMIN_USERNAME
                admin.set_login_password(Config.ADMIN_PASSWORD)
            elif Config.ALLOW_DEFAULT_ADMIN:
                admin.login_name = "admin"
                admin.set_login_password("admin123")
                logger.warning("已创建默认管理员账号: admin / admin123，请仅在本地开发使用")
            else:
                admin = None

            if admin is not None:
                admin.display_name = "管理员"
                admin.is_admin = True
                db.session.add(admin)
                db.session.commit()
                logger.info("已创建管理员账号")
            else:
                logger.warning(
                    "未发现管理员账号。请设置 ADMIN_USERNAME 和 ADMIN_PASSWORD 后重启应用。"
                )

        if is_cache_stale():
            try:
                refresh_skin_cache()
            except Exception as e:
                logger.warning(f"启动时皮肤缓存刷新失败: {e}")

    from scheduler import init_scheduler

    init_scheduler(app)

    # ============ 公开路由 ============

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return render_template("landing.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "GET":
            return render_template("login.html")

        login_name = request.form.get("login_name", "").strip()
        password = request.form.get("password", "").strip()
        user = User.query.filter_by(login_name=login_name).first()

        if not user or not user.check_login_password(password):
            flash(_("flash_login_error"), "danger")
            return render_template("login.html")

        login_user(user, remember=True)
        next_page = request.args.get("next")
        if next_page and is_safe_redirect_url(next_page):
            return redirect(next_page)
        return redirect(url_for("dashboard"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "GET":
            return render_template("register.html")

        login_name = request.form.get("login_name", "").strip()
        password = request.form.get("password", "").strip()
        password2 = request.form.get("password2", "").strip()
        display_name = request.form.get("display_name", "").strip() or login_name

        if not login_name or not password:
            flash(_("flash_register_fill"), "danger")
            return render_template("register.html")
        if password != password2:
            flash(_("flash_register_mismatch"), "danger")
            return render_template("register.html")
        if len(password) < 6:
            flash(_("flash_register_short"), "danger")
            return render_template("register.html")
        if User.query.filter_by(login_name=login_name).first():
            flash(_("flash_register_exists"), "danger")
            return render_template("register.html")

        user = User()
        user.login_name = login_name
        user.display_name = display_name
        user.set_login_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        flash(_("flash_register_ok"), "success")
        return redirect(url_for("bind_riot"))

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash(_("flash_logout"), "info")
        return redirect(url_for("login"))

    # ============ 用户路由 ============

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = _current_user()
        user_id = _current_user_id()
        today = date.today()
        offers = []
        accessory_offers = []
        accessory_expires_at = None
        favorite_matches = []
        fav_uuids = set()
        if user.riot_bound:
            offers_db = StoreOffer.query.filter_by(
                user_id=user_id, offer_date=today
            ).all()
            fav_uuids = {
                f.skin_uuid for f in Favorite.query.filter_by(user_id=user_id).all()
            }
            for o in offers_db:
                skin = db.session.get(Skin, o.skin_uuid)
                offers.append(
                    {
                        "uuid": o.skin_uuid,
                        "skin": skin,
                        "name": skin.name if skin else "未知皮肤",
                        "icon_url": skin.icon_url if skin else None,
                        "tier_name": skin.tier_name if skin else None,
                        "cost": o.cost,
                        "is_favorite": o.skin_uuid in fav_uuids,
                    }
                )
            favorite_matches = [offer for offer in offers if offer["is_favorite"]]
            accessory_rows = AccessoryOffer.query.filter_by(user_id=user_id).order_by(
                AccessoryOffer.id
            ).all()
            for accessory in accessory_rows:
                item_type = accessory.item_type
                name = accessory.name
                icon_url = accessory.icon_url
                # 兼容旧缓存或元数据服务曾经未命中的记录；只修正本次展示，
                # 下一次刷新商店时会用最新元数据覆盖数据库中的旧值。
                if item_type == "unknown" or not icon_url:
                    metadata = get_accessory_metadata(
                        accessory.item_type_uuid, accessory.item_uuid
                    )
                    item_type = metadata.get("item_type") or item_type
                    name = metadata.get("name") or name
                    icon_url = metadata.get("icon_url") or icon_url
                accessory_offers.append(
                    {
                        "uuid": accessory.item_uuid,
                        "item_type": item_type,
                        "name": name,
                        "icon_url": icon_url,
                        "cost": accessory.cost,
                    }
                )
            if accessory_rows:
                accessory_expires_at = accessory_rows[0].expires_at
        fav_count = Favorite.query.filter_by(user_id=user_id).count()
        return render_template(
            "dashboard.html",
            offers=offers,
            accessory_offers=accessory_offers,
            accessory_expires_at=accessory_expires_at,
            favorite_matches=favorite_matches,
            fav_count=fav_count,
            today=today,
        )

    @app.route("/bind", methods=["GET"])
    @login_required
    def bind_riot():
        return render_template("bind.html", regions=REGIONS)

    @app.route("/bind/url", methods=["POST"])
    @login_required
    def bind_url():
        user = _current_user()
        access_url = request.form.get("access_url", "").strip()
        region = request.form.get("region", "ap")
        valid_regions = {code for code, _ in REGIONS}

        if not access_url:
            flash(_("flash_bind_no_url"), "danger")
            return redirect(url_for("bind_riot"))
        if region not in valid_regions:
            flash("无效的区服选择", "danger")
            return redirect(url_for("bind_riot"))

        try:
            access_token, puuid_from_url = RiotAuth.parse_from_url(access_url)
            # 尝试验证一下 token 是否有效
            auth = RiotAuth("", "", region)
            auth.authorize_with_token(access_token)

            # 优先使用 userinfo 返回的 sub（更可靠）；URL 里的 id_token 仅作兜底
            puuid = auth.puuid or puuid_from_url
            if not puuid:
                raise AuthenticationError("无法解析 PUUID，请重新获取 URL 后再试")

            # 注意：此方法无法获取用户名，只能通过 PUUID 绑定
            # 我们尽量从 userinfo 获取用户名
            try:
                headers = {"Authorization": f"Bearer {access_token}"}
                resp = requests.get(
                    "https://auth.riotgames.com/userinfo", headers=headers, timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # 尝试多种可能的字段获取游戏名
                    game_name = data.get("acct", {}).get("game_name")
                    tag_line = data.get("acct", {}).get("tag_line")

                    if not game_name:
                        # 另一种常见的响应格式
                        game_name = data.get("game_name")
                        tag_line = data.get("tag_line")

                    if game_name:
                        user.riot_username = (
                            f"{game_name}#{tag_line}" if tag_line else game_name
                        )
                    else:
                        user.riot_username = f"User_{puuid[:8]}"
                else:
                    user.riot_username = f"User_{puuid[:8]}"
            except Exception as e:
                logger.warning(f"获取用户信息失败: {e}")
                user.riot_username = f"User_{puuid[:8]}"

            user.puuid = puuid
            # 尝试自动识别 shard（na/eu/ap）；失败则保留用户选择
            detected_shard = None
            try:
                detected_shard = detect_shard_by_token(access_token)
            except Exception as e:
                logger.warning(f"URL 绑定时自动识别区服失败: {e}")
            # Keep specific regional selections (KR, BR, TR, etc.) for the
            # notification time-zone default; only refine broad shard choices.
            user.region = (
                detected_shard
                if region in {"na", "eu", "ap"}
                and detected_shard in {"na", "eu", "ap"}
                else region
            )
            if user.notification_timezone_auto:
                user.notification_timezone = REGION_TIMEZONES.get(
                    user.region, user.notification_timezone
                )
            user.set_url_access_token(access_token)
            db.session.commit()

        except Exception as e:
            flash(_("flash_bind_failed", error=e), "danger")
            return redirect(url_for("bind_riot"))

        # 绑定已提交，商店刷新失败不应影响绑定结果
        try:
            store_data = get_user_store(
                user,
                access_token=access_token,
                entitlements_token=auth.entitlements_token,
                puuid=puuid,
            )
            if store_data.get("error"):
                flash(
                    _("flash_bind_ok_store_fail", error=store_data["error"]), "warning"
                )
            else:
                from webhook import notify_daily_store

                notify_daily_store(user, store_data)
                flash(_("flash_bind_ok"), "success")
        except Exception as e:
            logger.warning(f"URL 绑定后首次商店刷新异常: {e}")
            flash(_("flash_bind_ok_no_store"), "warning")
        return redirect(url_for("dashboard"))

    @app.route("/unbind", methods=["POST"])
    @login_required
    def unbind_riot():
        user = _current_user()
        user.riot_username = None
        user.encrypted_riot_password = None
        user.region = None
        user.puuid = None
        db.session.commit()
        flash(_("flash_unbind_ok"), "info")
        return redirect(url_for("dashboard"))

    @app.route("/my/store/refresh", methods=["POST"])
    @login_required
    def my_store_refresh():
        user = _current_user()
        if not user.riot_bound:
            flash(_("flash_not_bound"), "warning")
            return redirect(url_for("bind_riot"))
        from webhook import notify_daily_store, notify_store_error

        try:
            store_data = get_user_store(user)
        except Exception as exc:
            logger.exception("用户 %s 手动刷新商店发生异常", user.login_name)
            store_data = {"error": f"商店刷新异常: {exc}"}
        if store_data.get("error"):
            flash(_("flash_store_error", error=store_data["error"]), "danger")
            delivery = notify_store_error(user, str(store_data["error"]))
        else:
            delivery = notify_daily_store(user, store_data)
            match_count = len(store_data.get("favorites_matched") or [])
            if match_count:
                flash(_("flash_store_match", count=match_count), "success")
            else:
                flash(_("flash_store_ok"), "success")
        if delivery["attempted"]:
            category = "info" if delivery["failed"] == 0 else "warning"
            flash(
                _(
                    "flash_webhook_delivery",
                    success=delivery["success"],
                    failed=delivery["failed"],
                ),
                category,
            )
        return redirect(url_for("dashboard"))

    # --- 皮肤浏览 ---
    @app.route("/skins")
    @login_required
    def skins_list():
        user_id = _current_user_id()
        query = request.args.get("q", "")
        page = request.args.get("page", 1, type=int)
        pagination = search_skins(query, page=page, per_page=24)
        fav_uuids = {
            f.skin_uuid for f in Favorite.query.filter_by(user_id=user_id).all()
        }

        return render_template(
            "skins.html",
            skins=pagination.items,
            pagination=pagination,
            query=query,
            fav_uuids=fav_uuids,
        )

    # --- 收藏管理 ---
    @app.route("/my/favorites")
    @login_required
    def my_favorites():
        user_id = _current_user_id()
        favs = Favorite.query.filter_by(user_id=user_id).all()
        current_offer_uuids = {
            offer.skin_uuid
            for offer in StoreOffer.query.filter_by(
                user_id=user_id, offer_date=date.today()
            ).all()
        }
        return render_template(
            "favorites.html",
            favorites=favs,
            current_offer_uuids=current_offer_uuids,
        )

    @app.route("/my/favorites/toggle", methods=["POST"])
    @login_required
    def favorite_toggle():
        user_id = _current_user_id()
        payload = request.get_json(silent=True)
        skin_uuid = request.form.get("skin_uuid")
        if not skin_uuid and isinstance(payload, dict):
            value = payload.get("skin_uuid")
            skin_uuid = value if isinstance(value, str) else None
        if not skin_uuid:
            return jsonify({"error": "缺少皮肤UUID"}), 400
        if not db.session.get(Skin, skin_uuid):
            return jsonify({"error": "皮肤不存在"}), 404

        existing = Favorite.query.filter_by(
            user_id=user_id, skin_uuid=skin_uuid
        ).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
            return jsonify({"status": "removed"})
        else:
            fav = Favorite()
            fav.user_id = user_id
            fav.skin_uuid = skin_uuid
            db.session.add(fav)
            db.session.commit()
            matched_now = (
                StoreOffer.query.filter_by(
                    user_id=user_id, skin_uuid=skin_uuid, offer_date=date.today()
                ).first()
                is not None
            )
            return jsonify({"status": "added", "matched_now": matched_now})

    @app.route("/my/webhook", methods=["POST"])
    @login_required
    def my_webhook_save():
        user = _current_user()
        webhook_url = request.form.get("webhook_url", "").strip()
        if webhook_url:
            try:
                validate_webhook_url(webhook_url)
            except ValueError as exc:
                flash(_("flash_webhook_invalid", error=exc), "danger")
                return redirect(url_for("my_favorites"))
        user.webhook_url = webhook_url or None
        db.session.commit()
        flash(
            _("flash_webhook_saved" if webhook_url else "flash_webhook_removed"),
            "success",
        )
        return redirect(url_for("my_favorites"))

    @app.route("/my/webhook/test", methods=["POST"])
    @login_required
    def my_webhook_test():
        user = _current_user()
        if not user.webhook_url:
            flash(_("flash_webhook_missing"), "warning")
            return redirect(url_for("my_favorites"))
        from webhook import send_webhook

        payload = {
            "event": "test",
            "user": user.display_name or user.login_name,
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        ok = send_webhook(user.webhook_url, payload)
        flash(
            _("flash_webhook_test_ok" if ok else "flash_webhook_test_fail"),
            "success" if ok else "danger",
        )
        return redirect(url_for("my_favorites"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def user_settings():
        user = _current_user()
        if request.method == "POST":
            language = request.form.get("notification_language", "")
            timezone_name = request.form.get("notification_timezone", "")
            reminder_time = request.form.get("notification_reminder_time", "")
            timezone_auto = request.form.get("notification_timezone_auto") == "1"
            if language not in SUPPORTED_LANGS:
                flash(_("settings_language_invalid"), "danger")
                return redirect(url_for("user_settings"))
            if timezone_auto:
                timezone_name = REGION_TIMEZONES.get(user.region, timezone_name)
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                flash(_("settings_timezone_invalid"), "danger")
                return redirect(url_for("user_settings"))
            try:
                datetime.strptime(reminder_time, "%H:%M")
            except ValueError:
                flash(_("settings_reminder_time_invalid"), "danger")
                return redirect(url_for("user_settings"))
            user.notification_language = language
            user.notification_timezone = timezone_name
            user.notification_timezone_auto = timezone_auto
            user.notification_reminder_time = reminder_time
            db.session.commit()
            flash(_("settings_saved"), "success")
            return redirect(url_for("user_settings"))
        return render_template(
            "settings.html", user=user, notification_timezones=sorted(available_timezones())
        )

    # ============ 管理员路由 ============

    @app.route("/admin")
    @admin_required
    def admin_index():
        user_count = User.query.count()
        bound_count = User.query.filter(User.riot_username.isnot(None)).count()
        webhook_count = WebhookConfig.query.filter_by(is_active=True).count()
        skin_count = Skin.query.count()
        today_offers = StoreOffer.query.filter_by(offer_date=date.today()).count()
        from scheduler import get_scheduler

        sched = get_scheduler()
        next_run = None
        if sched:
            job = sched.get_job("daily_store_check")
            if job and job.next_run_time:
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        return render_template(
            "admin/index.html",
            user_count=user_count,
            bound_count=bound_count,
            webhook_count=webhook_count,
            skin_count=skin_count,
            today_offers=today_offers,
            next_run=next_run,
        )

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        users = User.query.order_by(User.created_at.desc()).all()
        return render_template("admin/users.html", users=users)

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_user_delete(user_id):
        admin_id = _current_user_id()
        user = db.session.get(User, user_id)
        if user:
            if user.id == admin_id:
                flash("不能删除自己", "danger")
                return redirect(url_for("admin_users"))
            name = user.display_name or user.login_name
            db.session.delete(user)
            db.session.commit()
            flash(f"用户 {name} 已删除", "info")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/toggle-admin", methods=["POST"])
    @admin_required
    def admin_toggle_admin(user_id):
        admin_id = _current_user_id()
        user = db.session.get(User, user_id)
        if user and user.id != admin_id:
            user.is_admin = not user.is_admin
            db.session.commit()
            status = "管理员" if user.is_admin else "普通用户"
            flash(f"{user.display_name or user.login_name} 已设为{status}", "info")
        return redirect(url_for("admin_users"))

    @app.route("/admin/settings")
    @admin_required
    def admin_settings():
        webhooks = WebhookConfig.query.order_by(WebhookConfig.created_at.desc()).all()
        from scheduler import get_scheduler

        sched = get_scheduler()
        next_run = None
        if sched:
            job = sched.get_job("daily_store_check")
            if job and job.next_run_time:
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        return render_template(
            "admin/settings.html", webhooks=webhooks, next_run=next_run
        )

    @app.route("/admin/webhook/add", methods=["POST"])
    @admin_required
    def admin_webhook_add():
        name = request.form.get("name", "").strip()
        url = request.form.get("url", "").strip()
        if not name or not url:
            flash("请填写名称和 URL", "danger")
            return redirect(url_for("admin_settings"))
        try:
            validate_webhook_url(url)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("admin_settings"))
        wh = WebhookConfig()
        wh.name = name
        wh.url = url
        db.session.add(wh)
        db.session.commit()
        flash(f"Webhook '{name}' 已添加", "success")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/webhook/<int:wh_id>/delete", methods=["POST"])
    @admin_required
    def admin_webhook_delete(wh_id):
        wh = db.session.get(WebhookConfig, wh_id)
        if wh:
            db.session.delete(wh)
            db.session.commit()
            flash("Webhook 已删除", "info")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/webhook/<int:wh_id>/toggle", methods=["POST"])
    @admin_required
    def admin_webhook_toggle(wh_id):
        wh = db.session.get(WebhookConfig, wh_id)
        if wh:
            wh.is_active = not wh.is_active
            db.session.commit()
            status = "启用" if wh.is_active else "禁用"
            flash(f"Webhook 已{status}", "info")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/webhook/<int:wh_id>/test", methods=["POST"])
    @admin_required
    def admin_webhook_test(wh_id):
        wh = db.session.get(WebhookConfig, wh_id)
        if not wh:
            flash("Webhook 不存在", "danger")
            return redirect(url_for("admin_settings"))
        from webhook import send_webhook

        payload = {
            "event": "test",
            "timestamp": datetime.now().isoformat(),
        }
        ok = send_webhook(wh.url, payload)
        if ok:
            flash("测试消息发送成功!", "success")
        else:
            flash("测试消息发送失败，请检查 URL", "danger")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/check-now", methods=["POST"])
    @admin_required
    def admin_check_now():
        from webhook import process_all_users

        result = process_all_users()
        flash(
            "手动检查完成: "
            f"{result['success']} 成功, {result['error']} 失败, "
            f"{result['favorite_matches']} 个收藏命中, "
            f"{result['notifications_sent']} 条推送成功, "
            f"{result['notification_errors']} 条推送失败",
            "info",
        )
        return redirect(url_for("admin_settings"))

    @app.route("/admin/refresh-skins", methods=["POST"])
    @admin_required
    def admin_refresh_skins():
        count = refresh_skin_cache()
        flash(f"皮肤缓存已刷新，共 {count} 个皮肤", "success")
        return redirect(url_for("admin_settings"))

    return app
