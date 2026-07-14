from unittest.mock import patch

from flask import Flask

from accessory_cache import ACCESSORY_TYPES, get_accessory_metadata
from skin_cache import search_skins
from models import AccessoryOffer, Favorite, Skin, StoreOffer, User, db
from store_api import (
    KINGDOM_CREDITS_UUID,
    get_user_store,
    parse_accessory_offers,
    parse_accessory_remaining_seconds,
)


def main() -> None:
    expected_types = {
        "dd3bf334-87f3-40bd-b043-682a57a8dc3a": ("buddy", "buddies"),
        "3f296c07-64c3-494c-923b-fe692a4fa1bd": ("card", "playercards"),
        "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475": ("spray", "sprays"),
        "de7caa6b-adf7-4588-bbd1-143831e786c6": ("title", "playertitles"),
    }
    assert ACCESSORY_TYPES == expected_types
    with patch(
        "accessory_cache._index_endpoint",
        return_value={
            "spray-id": {
                "name": "Test Spray",
                "icon_url": "https://example.test/spray.png",
            }
        },
    ):
        spray = get_accessory_metadata(
            "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475", "spray-id"
        )
    assert spray["item_type"] == "spray"
    assert spray["name"] == "Test Spray"

    storefront = {
        "AccessoryStore": {
            "StorefrontID": "weekly-storefront",
            "AccessoryStoreRemainingDurationInSeconds": 172800.0,
            "AccessoryStoreOffers": [
                {
                    "ContractID": "contract-id",
                    "Offer": {
                        "OfferID": "offer-id",
                        "Cost": {KINGDOM_CREDITS_UUID: 4000},
                        "Rewards": [
                            {
                                "ItemTypeID": "3f296c07-64c3-494c-923b-fe692a4fa1bd",
                                "ItemID": "item-id",
                                "Quantity": 1,
                            }
                        ],
                    },
                }
            ],
        }
    }

    offers = parse_accessory_offers(storefront)
    assert len(offers) == 1
    assert offers[0]["offer_id"] == "offer-id"
    assert offers[0]["item_uuid"] == "item-id"
    assert offers[0]["cost"] == 4000
    assert offers[0]["currency_uuid"] == KINGDOM_CREDITS_UUID
    assert offers[0]["storefront_id"] == "weekly-storefront"
    assert parse_accessory_remaining_seconds(storefront) == 172800

    assert parse_accessory_offers({}) == []
    assert parse_accessory_remaining_seconds({}) == 0

    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User()
        user.login_name = "weekly-test"
        user.display_name = "Weekly Test"
        user.password_hash = "test"
        user.puuid = "test-puuid"
        user.region = "ap"
        skin = Skin(
            uuid="skin-level-id",
            name="测试皮肤",
            name_i18n=(
                '{"en": "Test Skin", "ja": "テストスキン", "ko": "테스트 스킨"}'
            ),
            icon_url="https://example.test/skin.png",
            tier_name="精选",
        )
        db.session.add_all([user, skin])
        db.session.commit()
        favorite = Favorite()
        favorite.user_id = user.id
        favorite.skin_uuid = skin.uuid
        db.session.add(favorite)
        db.session.commit()

        storefront["SkinsPanelLayout"] = {
            "SingleItemOffers": ["skin-level-id"],
            "SingleItemStoreOffers": [
                {
                    "OfferID": "skin-level-id",
                    "Cost": {"85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741": 1775},
                }
            ],
            "SingleItemOffersRemainingDurationInSeconds": 3600,
        }
        metadata = {
            "item_type": "card",
            "name": "Test Player Card",
            "icon_url": "https://example.test/card.png",
        }
        with (
            patch("store_api.detect_shard_by_token", return_value="ap"),
            patch("store_api.fetch_storefront", return_value=storefront),
            patch("store_api.get_accessory_metadata", return_value=metadata),
        ):
            result = get_user_store(
                user,
                access_token="access-token",
                entitlements_token="entitlements-token",
                puuid="test-puuid",
            )

        assert result["error"] is None
        assert len(result["offers"]) == 1
        assert len(result["favorites_matched"]) == 1
        assert result["favorites_matched"][0]["uuid"] == skin.uuid
        assert len(result["accessory_offers"]) == 1
        assert StoreOffer.query.count() == 1
        saved = AccessoryOffer.query.one()
        assert saved.name == "Test Player Card"
        assert saved.cost == 4000
        assert saved.expires_at is not None

        assert search_skins("测试", per_page=10).total == 1
        assert search_skins("test skin", per_page=10).total == 1
        assert search_skins("テスト", per_page=10).total == 1
    print("accessory store parser tests passed")


if __name__ == "__main__":
    main()
