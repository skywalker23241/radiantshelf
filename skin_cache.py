import json
import logging
from datetime import datetime
from typing import Dict, Set

import requests

from models import Skin, db

logger = logging.getLogger(__name__)

SKINS_URL = "https://valorant-api.com/v1/weapons/skins"
WEAPONS_URL = "https://valorant-api.com/v1/weapons"
CONTENT_TIERS_URL = "https://valorant-api.com/v1/contenttiers"
SKIN_LEVEL_URL = "https://valorant-api.com/v1/weapons/skinlevels/{uuid}"

MELEE_WEAPON_UUID = "2f59173c-4bed-b6c3-2191-dea9b58be9c7"

# 应用语言 -> valorant-api.com 语言代码
LANG_MAP = {
    "zh": "zh-CN",
    "en": "en-US",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "pt": "pt-BR",
    "es": "es-ES",
    "tr": "tr-TR",
    "ru": "ru-RU",
}

PRIMARY_LANG = "zh"

# Valorant API Content Tier UUIDs to Base Prices (Guns)
TIER_PRICES_GLOBAL = {
    "12683d76-48d7-84a3-4e09-6985794f0445": 875,  # Select
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": 1275,  # Deluxe
    "60bca009-4182-7998-dee7-b8a2558dc369": 1775,  # Premium
    "e046854e-406c-37f4-6607-19a9ba8426fc": 2175,  # Exclusive
    "411e4a55-4e59-7757-41f0-86a53f101bb5": 2475,  # Ultra
}

TIER_PRICES_CN = {
    "12683d76-48d7-84a3-4e09-6985794f0445": 590,  # Select
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": 890,  # Deluxe
    "60bca009-4182-7998-dee7-b8a2558dc369": 1290,  # Premium
    "e046854e-406c-37f4-6607-19a9ba8426fc": 1590,  # Exclusive
    "411e4a55-4e59-7757-41f0-86a53f101bb5": 1990,  # Ultra
}

TIER_DISPLAY_NAMES = {
    "12683d76-48d7-84a3-4e09-6985794f0445": "精选",
    "0cebb8be-46d7-c12a-d306-e9907bfc5a25": "豪华",
    "60bca009-4182-7998-dee7-b8a2558dc369": "尊享",
    "e046854e-406c-37f4-6607-19a9ba8426fc": "独家",
    "411e4a55-4e59-7757-41f0-86a53f101bb5": "至臻",
}


def _fetch_skin_names(api_lang: str) -> Dict[str, str]:
    """获取指定语言的皮肤 level UUID -> displayName 映射"""
    try:
        resp = requests.get(SKINS_URL, params={"language": api_lang}, timeout=60)
        resp.raise_for_status()
        result = {}
        for weapon_skin in resp.json().get("data", []):
            levels = weapon_skin.get("levels", [])
            if not levels:
                continue
            base = levels[0]
            uuid = base.get("uuid")
            name = base.get("displayName")
            if uuid and name:
                result[uuid] = name
        return result
    except Exception as e:
        logger.warning(f"获取 {api_lang} 皮肤名称失败: {e}")
        return {}


def refresh_skin_cache():
    logger.info("正在刷新皮肤缓存...")

    # 1. 获取近战武器皮肤 UUID 集合
    melee_skin_uuids: Set[str] = set()
    try:
        resp = requests.get(WEAPONS_URL, timeout=30)
        resp.raise_for_status()
        for weapon in resp.json().get("data", []):
            if weapon["uuid"] == MELEE_WEAPON_UUID:
                for skin in weapon.get("skins", []):
                    melee_skin_uuids.add(skin["uuid"])
                break
    except Exception as e:
        logger.warning(f"获取武器列表失败: {e}")

    # 2. 获取皮肤等级信息
    tiers = {}
    try:
        resp = requests.get(
            CONTENT_TIERS_URL, params={"language": LANG_MAP[PRIMARY_LANG]}, timeout=30
        )
        resp.raise_for_status()
        for tier in resp.json().get("data", []):
            tiers[tier["uuid"]] = {
                "name": TIER_DISPLAY_NAMES.get(
                    tier["uuid"], tier.get("devName", "未知")
                ),
                "icon": tier.get("displayIcon"),
            }
    except Exception as e:
        logger.warning(f"获取皮肤等级数据失败: {e}")

    # 3. 获取主语言皮肤数据
    api_lang = LANG_MAP[PRIMARY_LANG]
    try:
        resp = requests.get(SKINS_URL, params={"language": api_lang}, timeout=60)
        resp.raise_for_status()
        skins_data = resp.json().get("data", [])
    except Exception as e:
        logger.error(f"获取皮肤数据失败: {e}")
        return 0

    # 4. 获取其他语言的皮肤名称
    i18n_names: Dict[str, Dict[str, str]] = {}
    for lang, api_code in LANG_MAP.items():
        if lang == PRIMARY_LANG:
            continue
        names = _fetch_skin_names(api_code)
        for uuid, name in names.items():
            i18n_names.setdefault(uuid, {})[lang] = name

    count = 0
    now = datetime.utcnow()

    for weapon_skin in skins_data:
        weapon_name = ""
        display_name = weapon_skin.get("displayName", "")
        if " " in display_name:
            parts = display_name.rsplit(" ", 1)
            if len(parts) == 2:
                weapon_name = parts[-1]

        is_melee = weapon_skin.get("uuid") in melee_skin_uuids
        tier_uuid = weapon_skin.get("contentTierUuid")
        tier_info = tiers.get(tier_uuid, {})

        # 刀皮价格通常是同品质枪皮的 2 倍
        price_multiplier = 2 if is_melee else 1

        # 修正：Select 级别的近战武器国际服价格通常是 1750 (875*2)
        # 修正：Exclusive 级别的近战武器国际服价格通常是 4350 (2175*2)
        base_price_global = TIER_PRICES_GLOBAL.get(tier_uuid)
        base_price_cn = TIER_PRICES_CN.get(tier_uuid)

        tier_price_global = (
            base_price_global * price_multiplier if base_price_global else None
        )
        tier_price_cn = base_price_cn * price_multiplier if base_price_cn else None

        levels = weapon_skin.get("levels", [])
        if not levels:
            continue

        base_level = levels[0]
        uuid = base_level.get("uuid")
        if not uuid:
            continue

        translations = i18n_names.get(uuid, {})
        i18n_json = (
            json.dumps(translations, ensure_ascii=False) if translations else None
        )

        skin = db.session.get(Skin, uuid)
        if skin:
            skin.name = base_level.get("displayName", display_name)
            skin.name_i18n = i18n_json
            skin.icon_url = base_level.get("displayIcon")
            skin.tier_name = tier_info.get("name")
            skin.tier_icon = tier_info.get("icon")
            skin.cost = tier_price_global
            skin.cost_cn = tier_price_cn
            skin.weapon_name = weapon_name
            skin.is_melee = is_melee
            skin.updated_at = now
        else:
            skin = Skin(
                uuid=uuid,
                name=base_level.get("displayName", display_name),
                name_i18n=i18n_json,
                icon_url=base_level.get("displayIcon"),
                tier_name=tier_info.get("name"),
                tier_icon=tier_info.get("icon"),
                cost=tier_price_global,
                cost_cn=tier_price_cn,
                weapon_name=weapon_name,
                is_melee=is_melee,
                updated_at=now,
            )
            db.session.add(skin)
        count += 1

    db.session.commit()
    logger.info(f"皮肤缓存刷新完成，共 {count} 个皮肤")
    return count


def get_skin(uuid: str):
    skin = db.session.get(Skin, uuid)
    if skin:
        return skin

    try:
        resp = requests.get(
            SKIN_LEVEL_URL.format(uuid=uuid),
            params={"language": LANG_MAP[PRIMARY_LANG]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if data:
            skin = Skin(
                uuid=uuid,
                name=data.get("displayName", "未知皮肤"),
                icon_url=data.get("displayIcon"),
                updated_at=datetime.utcnow(),
            )
            db.session.add(skin)
            db.session.commit()
            return skin
    except Exception as e:
        logger.warning(f"获取皮肤 {uuid} 数据失败: {e}")

    return None


def is_cache_stale() -> bool:
    latest = db.session.query(Skin.updated_at).order_by(Skin.updated_at.desc()).first()
    if not latest or not latest[0]:
        return True
    delta = datetime.utcnow() - latest[0]
    return delta.total_seconds() > 86400


def search_skins(query: str, page: int = 1, per_page: int = 24):
    q = Skin.query
    if query:
        q = q.filter(Skin.name.ilike(f"%{query}%"))
    q = q.filter(Skin.tier_name.isnot(None))
    return q.order_by(Skin.name).paginate(page=page, per_page=per_page, error_out=False)
