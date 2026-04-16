import json
import os
from typing import Any, Dict, Optional

from flask import Flask, request, session
from markupsafe import Markup

SUPPORTED_LANGS = ["zh", "en", "ja", "ko", "pt", "es", "tr", "ru"]
DEFAULT_LANG = "zh"
LANG_NAMES: Dict[str, str] = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "pt": "Português",
    "es": "Español",
    "tr": "Türkçe",
    "ru": "Русский",
}

_translations: Dict[str, Dict[str, str]] = {}


def load_translations():
    translations_dir = os.path.join(os.path.dirname(__file__), "translations")
    for lang in SUPPORTED_LANGS:
        filepath = os.path.join(translations_dir, f"{lang}.json")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                _translations[lang] = json.load(f)


def get_locale() -> str:
    lang = session.get("lang")
    if lang in SUPPORTED_LANGS:
        return lang
    best = request.accept_languages.best_match(SUPPORTED_LANGS)
    return best or DEFAULT_LANG


def translate(key: str, **kwargs: Any) -> str:
    lang = get_locale()
    text = _translations.get(lang, {}).get(key)
    if text is None:
        text = _translations.get(DEFAULT_LANG, {}).get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def localized_skin_name(skin) -> str:
    """根据当前语言返回皮肤名称"""
    lang = get_locale()
    if lang != DEFAULT_LANG and skin.name_i18n:
        try:
            names = json.loads(skin.name_i18n)
            if lang in names:
                return names[lang]
        except (json.JSONDecodeError, TypeError):
            pass
    return skin.name


def localized_tier_name(tier_name: Optional[str]) -> str:
    """根据当前语言返回皮肤等级名称"""
    if not tier_name:
        return ""

    # 归一化映射：将可能的英文名称映射到内部使用的中文键
    normalization = {
        "Select": "精选",
        "Deluxe": "豪华",
        "Premium": "尊享",
        "Exclusive": "独家",
        "Ultra": "至臻",
        "EXCLUSIVE": "独家",
        "SELECT": "精选",
        "DELUXE": "豪华",
        "PREMIUM": "尊享",
        "ULTRA": "至臻",
        "Exclusive Edition": "至臻",
    }

    internal_name = normalization.get(tier_name, tier_name)

    lang = get_locale()
    key = f"tier_{internal_name}"
    translated = _translations.get(lang, {}).get(key)
    if translated:
        return translated
    return tier_name


def init_i18n(app: Flask):
    load_translations()

    @app.before_request
    def set_locale():
        if "lang" in request.args:
            lang = request.args["lang"]
            if lang in SUPPORTED_LANGS:
                session["lang"] = lang
        if "price_region" in request.args:
            region = request.args["price_region"]
            if region in ["cn", "global"]:
                session["price_region"] = region

    def get_price_region():
        return session.get("price_region", "cn")

    def localized_price(skin):
        if not skin:
            return None
        region = get_price_region()
        if region == "cn":
            return skin.cost_cn
        return skin.cost

    @app.context_processor
    def inject_i18n():
        return {
            "_": translate,
            "skin_name": localized_skin_name,
            "tier_display": localized_tier_name,
            "localized_price": localized_price,
            "current_lang": get_locale(),
            "current_price_region": get_price_region(),
            "supported_langs": SUPPORTED_LANGS,
            "lang_names": LANG_NAMES,
        }
