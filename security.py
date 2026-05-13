import ipaddress
import secrets
import socket
from urllib.parse import urlparse

from flask import abort, request, session

from config import Config

CSRF_SESSION_KEY = "_csrf_token"


def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_token() -> None:
    expected = session.get(CSRF_SESSION_KEY)
    supplied = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRFToken")
        or request.headers.get("X-CSRF-Token")
    )
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400, description="Invalid CSRF token")


def is_safe_redirect_url(target: str) -> bool:
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.netloc and not parsed.scheme and target.startswith("/")


def _host_is_private(hostname: str) -> bool:
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True

    for family, _, _, _, sockaddr in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Webhook URL 必须是有效的 HTTP 或 HTTPS 地址")
    if not Config.ALLOW_PRIVATE_WEBHOOKS and _host_is_private(parsed.hostname):
        raise ValueError("Webhook URL 不能指向本机、内网或保留地址")
