"""Create a strict, structure-only summary from a HAR capture.

The output deliberately omits every header value, query value, cookie value,
body scalar value, timestamp, IP address, redirect target, and HAR extension.
It is intended for local use before a capture is shared for API-shape analysis.
It does not make capturing or accessing a private API authorized.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


FORMAT_NAME = "radiantshelf-sanitized-har"
FORMAT_VERSION = 1

_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1",
    b"\xa1\xb2\xc3\xd4",
    b"\x4d\x3c\xb2\xa1",
    b"\xa1\xb2\x3c\x4d",
    b"\x0a\x0d\x0d\x0a",  # pcapng
}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_LONG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+=/.-]{16,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LONG_NUMBER_RE = re.compile(r"^\d{5,}$")

_IDENTIFIER_CONTEXTS = {
    "account",
    "accounts",
    "credential",
    "device",
    "devices",
    "openid",
    "player",
    "players",
    "profile",
    "profiles",
    "puuid",
    "session",
    "sessions",
    "ticket",
    "token",
    "uin",
    "unionid",
    "user",
    "users",
}

_SENSITIVE_FIELD_PARTS = {
    "accesstoken",
    "authorization",
    "cookie",
    "credential",
    "csrf",
    "deviceid",
    "email",
    "idtoken",
    "imei",
    "installid",
    "macaddress",
    "oaid",
    "openid",
    "password",
    "pskey",
    "pt4token",
    "qrsig",
    "refreshtoken",
    "secret",
    "sessionid",
    "signature",
    "skey",
    "ticket",
    "token",
    "unionid",
    "xsrf",
}


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _looks_sensitive_name(value: str) -> bool:
    normalized = _normalized_name(value)
    return any(part in normalized for part in _SENSITIVE_FIELD_PARTS)


def _looks_like_identifier(value: str) -> bool:
    if not value:
        return False
    return bool(
        _UUID_RE.fullmatch(value)
        or _JWT_RE.fullmatch(value)
        or _EMAIL_RE.fullmatch(value)
        or _LONG_NUMBER_RE.fullmatch(value)
        or _LONG_TOKEN_RE.fullmatch(value)
    )


def _safe_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "<field>"
    if _looks_like_identifier(value) or len(value) > 80:
        return "<field>"
    return value


def _sanitize_host(host: str | None) -> str:
    if not host:
        return "<unknown-host>"
    labels = []
    for label in host.split("."):
        labels.append("<id>" if _looks_like_identifier(label) else label[:63])
    return ".".join(labels)


def _sanitize_path(path: str) -> str:
    decoded = unquote(path or "/")
    raw_segments = decoded.split("/")
    sanitized: list[str] = []
    previous = ""
    for segment in raw_segments:
        normalized_previous = _normalized_name(previous)
        should_redact = (
            _looks_like_identifier(segment)
            or normalized_previous in _IDENTIFIER_CONTEXTS
            or len(segment) > 80
        )
        sanitized.append("<id>" if segment and should_redact else segment)
        previous = segment
    result = "/".join(sanitized)
    return result if result.startswith("/") else f"/{result}"


def _sanitize_url(url: str) -> tuple[str, list[str]]:
    try:
        parsed = urlsplit(url)
    except Exception:
        return "<invalid-url>", []
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "<scheme>"
    endpoint = f"{scheme}://{_sanitize_host(parsed.hostname)}{_sanitize_path(parsed.path)}"
    query_names: list[str] = []
    if parsed.query:
        for pair in parsed.query.split("&"):
            name = pair.split("=", 1)[0]
            if name:
                query_names.append(_safe_name(unquote(name)))
    return endpoint, sorted(set(query_names))


def _base_mime(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.split(";", 1)[0].strip().lower()[:100]


def _scalar_shape(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "unknown"


def _shape(value: Any, *, depth: int = 0) -> Any:
    """Return keys and container types without retaining scalar values."""
    if depth >= 8:
        return "<max-depth>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 100:
                result["<additional-fields>"] = "removed"
                break
            key = _safe_name(raw_key)
            if isinstance(raw_key, str) and _looks_sensitive_name(raw_key):
                result[key] = "<removed>"
            else:
                result[key] = _shape(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        item_shapes: list[Any] = []
        seen: set[str] = set()
        for item in value[:20]:
            item_shape = _shape(item, depth=depth + 1)
            signature = json.dumps(item_shape, sort_keys=True, ensure_ascii=True)
            if signature not in seen:
                item_shapes.append(item_shape)
                seen.add(signature)
            if len(item_shapes) >= 3:
                break
        return {"type": "array", "items": item_shapes}
    return _scalar_shape(value)


def _json_shape(text: Any, encoding: Any = None) -> Any | None:
    if not isinstance(text, str) or encoding == "base64":
        return None
    try:
        return _shape(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _header_names(headers: Any) -> list[str]:
    if not isinstance(headers, list):
        return []
    names = []
    for header in headers:
        if isinstance(header, dict) and header.get("name"):
            names.append(_safe_name(header["name"]).lower())
    return sorted(set(names))


def _query_names(request: dict[str, Any], parsed_names: list[str]) -> list[str]:
    names = list(parsed_names)
    query = request.get("queryString")
    if isinstance(query, list):
        for item in query:
            if isinstance(item, dict) and item.get("name"):
                names.append(_safe_name(item["name"]))
    return sorted(set(names))


def _request_body_summary(post_data: Any) -> dict[str, Any] | None:
    if not isinstance(post_data, dict):
        return None
    mime = _base_mime(post_data.get("mimeType"))
    summary: dict[str, Any] = {"kind": mime or "unknown"}
    shape = _json_shape(post_data.get("text"))
    if shape is not None:
        summary["schema"] = shape
        return summary
    params = post_data.get("params")
    if isinstance(params, list):
        names = [
            _safe_name(item.get("name"))
            for item in params
            if isinstance(item, dict) and item.get("name")
        ]
        summary["field_names"] = sorted(set(names))
    summary["content"] = "<opaque-body-removed>"
    return summary


def _response_body_summary(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, dict):
        return None
    mime = _base_mime(content.get("mimeType"))
    summary: dict[str, Any] = {"kind": mime or "unknown"}
    shape = _json_shape(content.get("text"), content.get("encoding"))
    if shape is not None:
        summary["schema"] = shape
    elif content.get("text") is not None or content.get("size"):
        summary["content"] = "<opaque-body-removed>"
    return summary


def _load_har_document(path: Path) -> Any:
    """Load JSON while identifying common non-HAR capture formats safely."""
    raw = path.read_bytes()
    if not raw:
        raise ValueError(
            "Input file is empty. Export the completed network capture as HAR/HTTP "
            "Archive with content."
        )

    prefix = raw[:16]
    if prefix[:4] in _PCAP_MAGICS:
        raise ValueError(
            "Input is a PCAP/PCAPNG packet capture, not HAR JSON. Export the relevant "
            "HTTP requests as a .har file first."
        )
    if prefix.startswith(b"PK\x03\x04"):
        raise ValueError(
            "Input is a ZIP/SAZ archive, not HAR JSON. Use the capture tool's "
            "'Export as HAR (HTTP Archive)' option."
        )
    if prefix.startswith(b"\x1f\x8b"):
        raise ValueError(
            "Input is gzip-compressed. Decompress it locally, then pass the resulting "
            ".har JSON file."
        )

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        # utf-8-sig accepts both ordinary UTF-8 and files with a UTF-8 BOM.
        encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(
            "Input is not UTF-8/UTF-16 HAR JSON; it may be a binary capture format."
        ) from exc

    stripped = text.lstrip()
    lowered_prefix = stripped[:32].lower()
    if lowered_prefix.startswith(("<!doctype", "<html", "<?xml", "<")):
        raise ValueError(
            "Input is HTML/XML rather than HAR JSON. Re-export the Network panel as "
            "HAR instead of saving the current page."
        )
    if not stripped.startswith(("{", "[")):
        raise ValueError(
            "Input is plain text or an unsupported capture format, not HAR JSON. "
            "Use 'Export/Save all as HAR with content'."
        )

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Input looks like JSON but is malformed at line {exc.lineno}, "
            f"column {exc.colno}. Re-export the HAR rather than editing it manually."
        ) from exc


def sanitize_har(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("HAR root must be an object")
    log = document.get("log")
    if not isinstance(log, dict) or not isinstance(log.get("entries"), list):
        raise ValueError("HAR must contain log.entries")

    entries: list[dict[str, Any]] = []
    for raw_entry in log["entries"]:
        if not isinstance(raw_entry, dict):
            continue
        request = raw_entry.get("request")
        response = raw_entry.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            continue

        endpoint, parsed_query_names = _sanitize_url(str(request.get("url", "")))
        request_summary: dict[str, Any] = {
            "method": str(request.get("method", "UNKNOWN"))[:16].upper(),
            "endpoint": endpoint,
            "query_names": _query_names(request, parsed_query_names),
            "header_names": _header_names(request.get("headers")),
        }
        body = _request_body_summary(request.get("postData"))
        if body is not None:
            request_summary["body"] = body

        status = response.get("status")
        response_summary: dict[str, Any] = {
            "status": status if isinstance(status, int) else None,
            "header_names": _header_names(response.get("headers")),
        }
        response_body = _response_body_summary(response.get("content"))
        if response_body is not None:
            response_summary["body"] = response_body

        entries.append({"request": request_summary, "response": response_summary})

    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "policy": "strict-structure-only",
        "warning": (
            "This summary omits values by design. Do not share or commit the raw HAR."
        ),
        "entry_count": len(entries),
        "entries": entries,
    }


def _write_json_safely(output: Path, payload: dict[str, Any], *, force: bool) -> None:
    if output.exists() and not force:
        raise FileExistsError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=output.parent, delete=False, suffix=".tmp"
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(serialized)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, output)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a strict structure-only JSON summary from a local HAR file."
    )
    parser.add_argument("input", type=Path, help="Raw HAR path (keep this file private)")
    parser.add_argument("-o", "--output", type=Path, help="Sanitized summary path")
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing sanitized output"
    )
    args = parser.parse_args()

    input_path = args.input.resolve()
    output_path = (
        args.output.resolve()
        if args.output
        else input_path.with_name(f"{input_path.stem}.sanitized.json")
    )
    if input_path == output_path:
        parser.error("Input and output paths must be different")

    try:
        document = _load_har_document(input_path)
        payload = sanitize_har(document)
        _write_json_safely(output_path, payload, force=args.force)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    print(f"Sanitized structure written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
