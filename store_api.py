import base64
import json
import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional, cast

import requests
from requests import Response

from config import REGION_TO_SHARD
from models import Favorite, StoreOffer, User, db
from riot_auth import AuthenticationError, RateLimitError, RiotAuth
from skin_cache import get_skin

logger = logging.getLogger(__name__)

CLIENT_PLATFORM = base64.b64encode(
    json.dumps(
        {
            "platformType": "PC",
            "platformOS": "Windows",
            "platformOSVersion": "10.0.19042.1.256.64bit",
            "platformChipset": "Unknown",
        }
    ).encode()
).decode()

VERSION_URL = "https://valorant-api.com/v1/version"
GEO_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"

_client_version = None


def get_client_version() -> str:
    global _client_version
    if _client_version:
        return _client_version
    try:
        resp = requests.get(VERSION_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        _client_version = data.get(
            "riotClientVersion", "release-09.08-shipping-11-2609530"
        )
        return _client_version
    except Exception as e:
        logger.warning(f"获取客户端版本失败: {e}")
        return "release-09.08-shipping-11-2609530"


def fetch_storefront(
    access_token: str, entitlements_token: str, puuid: str, shard: str
) -> Dict[str, Any]:
    urls = [
        ("GET", f"https://pd.{shard}.a.pvp.net/store/v2/storefront/{puuid}"),
        ("POST", f"https://pd.{shard}.a.pvp.net/store/v3/storefront/{puuid}"),
    ]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlements_token,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
        "X-Riot-ClientVersion": get_client_version(),
    }
    last_resp: Optional[Response] = None
    for method, url in urls:
        if method == "POST":
            resp = requests.post(url, headers=headers, json={}, timeout=15)
        else:
            resp = requests.get(url, headers=headers, timeout=15)
        last_resp = resp
        if resp.status_code == 429:
            raise RateLimitError("商店请求过于频繁")
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, dict):
                return cast(Dict[str, Any], payload)
            raise RuntimeError("商店响应格式异常: 不是 JSON 对象")
        # v2/v3 接口差异导致的常见情况：某个版本端点不存在
        if resp.status_code in (404, 405):
            continue
        resp.raise_for_status()

    # 两个版本端点都不可用时，保留最后一个响应信息用于上层诊断
    if last_resp is not None:
        last_resp.raise_for_status()
    raise RuntimeError("获取商店失败: 未获得有效响应")


def detect_shard_by_token(access_token: str) -> Optional[str]:
    """
    通过 Riot Geo 接口检测账号真实 shard（na/eu/ap）。
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    resp = requests.put(GEO_URL, headers=headers, json={}, timeout=10)
    resp.raise_for_status()
    data: Dict[str, Any] = resp.json()
    return (data.get("affinities") or {}).get("live")


def _ordered_shards(primary: str, detected: Optional[str] = None) -> List[str]:
    base = ["ap", "na", "eu"]
    ordered = []
    for s in [primary, detected, *base]:
        if s and s not in ordered:
            ordered.append(s)
    return ordered


def parse_daily_offers(storefront_data: Dict[str, Any]) -> List[str]:
    panel = storefront_data.get("SkinsPanelLayout")
    if not isinstance(panel, dict):
        return []
    offers = panel.get("SingleItemOffers")
    if not isinstance(offers, list):
        return []
    # 只保留字符串 UUID，避免类型检查器/运行时异常
    return [x for x in offers if isinstance(x, str)]


def parse_offers_remaining_seconds(storefront_data: Dict[str, Any]) -> int:
    panel = storefront_data.get("SkinsPanelLayout")
    if not isinstance(panel, dict):
        return 0
    remaining = panel.get("SingleItemOffersRemainingDurationInSeconds", 0)
    if isinstance(remaining, int):
        return remaining
    if isinstance(remaining, float):
        return int(remaining)
    return 0


def get_user_store(
    user: User,
    access_token: Optional[str] = None,
    entitlements_token: Optional[str] = None,
    puuid: Optional[str] = None,
) -> Dict[str, Any]:
    if not user.riot_bound and not (access_token and puuid):
        return {"error": "未绑定 Riot 账号", "offers": []}

    try:
        if not access_token or not entitlements_token or not puuid:
            saved_url_token, issued_ts = user.get_url_access_token()
            if saved_url_token:
                # Riot URL token 一般约 1 小时有效，这里预留少量缓冲
                if issued_ts is not None and (time.time() - issued_ts) > 55 * 60:
                    return {
                        "error": "登录凭据已过期，请重新绑定",
                        "offers": [],
                    }
                auth = RiotAuth("", "", user.region or "ap")
                access_token, entitlements_token, puuid = auth.authorize_with_token(
                    saved_url_token
                )
            else:
                return {
                    "error": "缺少登录凭据，请重新绑定",
                    "offers": [],
                }
    except AuthenticationError as e:
        logger.error(f"用户 {user.display_name or user.login_name} 认证失败: {e}")
        return {"error": str(e), "offers": []}
    except RateLimitError as e:
        logger.error(f"用户 {user.display_name or user.login_name} 速率限制: {e}")
        return {"error": str(e), "offers": []}
    except requests.HTTPError as e:
        status = e.response.status_code if e.response else None
        if status in (401, 403):
            return {
                "error": "登录凭据已失效，请重新绑定",
                "offers": [],
            }
        logger.error(f"认证过程 HTTP 错误: {e}")
        return {"error": f"认证失败: {e}", "offers": []}
    except Exception as e:
        logger.error(f"认证过程发生未知错误: {e}")
        return {"error": f"认证失败: {e}", "offers": []}

    # 经过上面的认证流程后，3 个值必须都存在；这里显式收窄类型给静态检查器
    if not access_token or not entitlements_token or not puuid:
        return {"error": "认证结果不完整，请重新绑定账号", "offers": []}

    if puuid and user.puuid != puuid:
        user.puuid = puuid
        db.session.commit()

    primary_shard = REGION_TO_SHARD.get(user.region or "na", "na")
    detected_shard = None
    try:
        detected_shard = detect_shard_by_token(access_token)
    except Exception as e:
        logger.warning(f"Geo 区服检测失败，将继续多分片重试: {e}")

    storefront: Optional[Dict[str, Any]] = None
    tried: List[str] = []
    last_err: Optional[Exception] = None
    for shard in _ordered_shards(primary_shard, detected_shard):
        tried.append(shard)
        try:
            storefront = fetch_storefront(access_token, entitlements_token, puuid, shard)
            if shard != primary_shard and shard in ("na", "eu", "ap"):
                user.region = shard
                db.session.commit()
                logger.info(
                    f"用户 {user.display_name or user.login_name} 区服自动修正: {primary_shard} -> {shard}"
                )
            break
        except requests.HTTPError as e:
            last_err = e
            status = e.response.status_code if e.response else None
            if status == 404:
                continue
            logger.error(f"获取商店数据失败: {e}")
            return {"error": str(e), "offers": []}
        except Exception as e:
            last_err = e
            logger.error(f"获取商店数据失败: {e}")
            return {"error": str(e), "offers": []}

    if storefront is None:
        detail = str(last_err) if last_err else "未知错误"
        return {
            "error": (
                f"所有分片重试均失败（已尝试: {', '.join(tried)}）。"
                f"最后错误: {detail}"
            ),
            "offers": [],
        }

    skin_uuids = parse_daily_offers(storefront)
    remaining = parse_offers_remaining_seconds(storefront)

    offers: List[Dict[str, Any]] = []
    today = date.today()
    if user.id is None:
        return {"error": "用户ID无效，请重新登录后重试", "offers": []}
    user_id = int(user.id)

    StoreOffer.query.filter_by(user_id=user_id, offer_date=today).delete()

    for skin_uuid in skin_uuids:
        skin = get_skin(skin_uuid)
        cost_value: Optional[int] = skin.cost if skin else None
        # 使用逐字段赋值，避免类型检查器对 SQLAlchemy 构造参数的误报
        offer = StoreOffer()
        offer.user_id = user_id
        offer.skin_uuid = skin_uuid
        offer.offer_date = today
        offer.cost = cost_value
        db.session.add(offer)

        offers.append(
            {
                "uuid": skin_uuid,
                "name": skin.name if skin else "未知皮肤",
                "icon_url": skin.icon_url if skin else None,
                "tier_name": skin.tier_name if skin else None,
                "tier_icon": skin.tier_icon if skin else None,
                "cost": cost_value,
                "weapon_name": skin.weapon_name if skin else None,
            }
        )

    db.session.commit()

    fav_uuids = {f.skin_uuid for f in Favorite.query.filter_by(user_id=user_id).all()}
    favorites_matched = [o for o in offers if o["uuid"] in fav_uuids]

    return {
        "offers": offers,
        "favorites_matched": favorites_matched,
        "remaining_seconds": remaining,
        "error": None,
    }
