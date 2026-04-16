import requests

WEAPONS_URL = "https://valorant-api.com/v1/weapons"
resp = requests.get(WEAPONS_URL)
weapons = resp.json().get("data", [])

for w in weapons:
    if w["displayName"] == "Melee" or "Melee" in w["assetPath"]:
        print(f"Melee Weapon UUID: {w['uuid']}")
        # 打印前3个皮肤作为参考
        for s in w["skins"][:3]:
            print(f"  - {s['displayName']}")
