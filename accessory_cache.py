"""Resolve weekly accessory-store reward UUIDs to display metadata."""

import logging
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://valorant-api.com/v1"
PRIMARY_LANGUAGE = "zh-CN"

ACCESSORY_TYPES: Dict[str, tuple[str, str]] = {
    "dd3bf334-87f3-40bd-b043-682a57a8dc3a": ("buddy", "buddies"),
    "3f296c07-64c3-494c-923b-fe692a4fa1bd": ("card", "playercards"),
    "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475": ("spray", "sprays"),
    "de7caa6b-adf7-4588-bbd1-143831e786c6": ("title", "playertitles"),
}

_metadata_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _index_endpoint(endpoint: str) -> Dict[str, Dict[str, Any]]:
    cached = _metadata_cache.get(endpoint)
    if cached is not None:
        return cached

    index: Dict[str, Dict[str, Any]] = {}
    try:
        response = requests.get(
            f"{API_ROOT}/{endpoint}",
            params={"language": PRIMARY_LANGUAGE},
            timeout=30,
        )
        response.raise_for_status()
        for item in response.json().get("data", []):
            if not isinstance(item, dict):
                continue
            name = item.get("displayName") or item.get("titleText") or "未知配件"
            icon = (
                item.get("displayIcon")
                or item.get("fullTransparentIcon")
                or item.get("fullIcon")
                or item.get("wideArt")
                or item.get("smallArt")
                or item.get("largeArt")
            )
            uuid = item.get("uuid")
            metadata = {"name": name, "icon_url": icon}
            if isinstance(uuid, str):
                index[uuid.lower()] = metadata

            # 挂饰和部分喷漆在商店奖励中使用 level UUID，而内容 API 的
            # 顶层对象使用基础 UUID，因此同时索引 levels。
            levels = item.get("levels")
            if isinstance(levels, list):
                for level in levels:
                    if not isinstance(level, dict):
                        continue
                    level_uuid = level.get("uuid")
                    if not isinstance(level_uuid, str):
                        continue
                    index[level_uuid.lower()] = {
                        "name": level.get("displayName") or name,
                        "icon_url": level.get("displayIcon") or icon,
                    }
    except Exception as exc:
        logger.warning("获取配件元数据失败 (%s): %s", endpoint, exc)

    _metadata_cache[endpoint] = index
    return index


def get_accessory_metadata(item_type_uuid: str, item_uuid: str) -> Dict[str, Any]:
    item_type, endpoint = ACCESSORY_TYPES.get(
        item_type_uuid.lower(), ("unknown", "")
    )
    if not endpoint:
        return {"item_type": item_type, "name": "未知配件", "icon_url": None}

    metadata = _index_endpoint(endpoint).get(item_uuid.lower(), {})
    return {
        "item_type": item_type,
        "name": metadata.get("name", "未知配件"),
        "icon_url": metadata.get("icon_url"),
    }
