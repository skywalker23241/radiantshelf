import ipaddress
import secrets
import socket
from urllib.parse import urlparse
from urllib.request import getproxies, proxy_bypass

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


def _request_uses_proxy(hostname: str, scheme: str) -> bool:
    """Return whether Requests will send this hostname through an HTTP proxy."""
    return bool(getproxies().get(scheme) and not proxy_bypass(hostname))


def _host_is_private(hostname: str, scheme: str) -> bool:
    normalized_host = hostname.rstrip(".").lower()
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
        or normalized_host.endswith(".internal")
    ):
        return True

    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None

    if address is not None:
        return (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )

    try:
        addresses = socket.getaddrinfo(normalized_host, None)
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
            # When Requests uses an HTTP(S) proxy, DNS may intentionally map
            # public hostnames to an internal proxy address. The proxy, not
            # this process, establishes the upstream connection, so rejecting
            # the mapped address would block valid public webhook endpoints.
            if not _request_uses_proxy(normalized_host, scheme):
                return True
    return False


def validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Discord Webhook URL 必须是有效的 HTTPS 地址")
    hostname = parsed.hostname.lower()
    valid_discord_host = hostname in {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    }
    path_parts = [part for part in parsed.path.split("/") if part]
    valid_discord_path = (
        len(path_parts) >= 4
        and path_parts[0] == "api"
        and path_parts[1] == "webhooks"
        and bool(path_parts[2])
        and bool(path_parts[3])
    )
    if parsed.scheme != "https" or not valid_discord_host or not valid_discord_path:
        raise ValueError("请填写有效的 Discord Webhook URL")
    if not Config.ALLOW_PRIVATE_WEBHOOKS and _host_is_private(
        parsed.hostname, parsed.scheme
    ):
        raise ValueError("Webhook URL 不能指向本机、内网或保留地址")
