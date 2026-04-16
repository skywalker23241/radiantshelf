import base64
import json
import logging
import re
import ssl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    pass


class RateLimitError(Exception):
    pass


ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
USERINFO_URL = "https://auth.riotgames.com/userinfo"

CIPHERS = [
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-CHACHA20-POLY1305",
    "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-RSA-CHACHA20-POLY1305",
    "TLS_CHACHA20_POLY1305_SHA256",
    "TLS_AES_128_GCM_SHA256",
    "TLS_AES_256_GCM_SHA384",
]

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


class SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context(ciphers=":".join(CIPHERS))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


class RiotAuth:
    def __init__(self, username: str, password: str, region: str = "ap"):
        self.username = username
        self.password = password
        self.region = region
        self.session = requests.Session()
        self.session.mount("https://", SSLAdapter())
        self.session.headers.update(
            {
                "User-Agent": "RiotClient/63.0.0.4239 rso-auth (Windows;10;;Professional, x64)",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Riot-ClientPlatform": CLIENT_PLATFORM,
            }
        )
        self.access_token = None
        self.entitlements_token = None
        self.puuid = None

    @staticmethod
    def parse_from_url(url: str):
        """
        从重定向 URL (如 playvalorant.com/opt_in#access_token=...) 中解析令牌
        """
        # 处理 fragments (# 后面)
        if "#" in url:
            fragment = url.split("#")[1]
        else:
            fragment = url

        access_token_match = re.search(r"access_token=([^&]+)", fragment)
        id_token_match = re.search(r"id_token=([^&]+)", fragment)

        if not access_token_match:
            raise AuthenticationError("无法在 URL 中找到 access_token")

        access_token = access_token_match.group(1)

        # 从 id_token 中提取 puuid (sub)
        puuid = None
        if id_token_match:
            id_token = id_token_match.group(1)
            try:
                # JWT 由 [header].[payload].[signature] 组成
                payload_b64 = id_token.split(".")[1]
                # 补齐 padding
                missing_padding = len(payload_b64) % 4
                if missing_padding:
                    payload_b64 += "=" * (4 - missing_padding)

                payload = json.loads(base64.b64decode(payload_b64).decode())
                puuid = payload.get("sub")
            except Exception as e:
                logger.warning(f"无法从 id_token 解析 PUUID: {e}")

        return access_token, puuid

    def authorize_with_token(self, access_token: str):
        """
        使用已有的 access_token 初始化会话并获取 entitlements
        """
        self.access_token = access_token
        self._get_entitlements()
        if not self.puuid:
            self._get_puuid()
        return self.access_token, self.entitlements_token, self.puuid

    def _get_entitlements(self):
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = self.session.post(ENTITLEMENTS_URL, headers=headers, json={})
        resp.raise_for_status()
        self.entitlements_token = resp.json()["entitlements_token"]

    def _get_puuid(self):
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = self.session.get(USERINFO_URL, headers=headers)
        resp.raise_for_status()
        self.puuid = resp.json()["sub"]
