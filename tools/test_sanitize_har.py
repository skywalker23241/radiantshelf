import json
import tempfile
import unittest
from pathlib import Path

from sanitize_har import _load_har_document, sanitize_har


class SanitizeHarTests(unittest.TestCase):
    def test_removes_secrets_from_every_value_location(self):
        secrets = [
            "HEADER_SENTINEL_9f3c",
            "COOKIE_SENTINEL_9f3c",
            "QUERY_SENTINEL_9f3c",
            "FRAGMENT_SENTINEL_9f3c",
            "REQUEST_SENTINEL_9f3c",
            "RESPONSE_SENTINEL_9f3c",
            "REDIRECT_SENTINEL_9f3c",
        ]
        har = {
            "log": {
                "creator": {"name": "private-browser"},
                "pages": [{"title": "private-title"}],
                "entries": [
                    {
                        "startedDateTime": "2026-07-14T12:34:56Z",
                        "serverIPAddress": "192.0.2.10",
                        "_webSocketMessages": [{"data": "WS_SENTINEL_9f3c"}],
                        "request": {
                            "method": "post",
                            "url": (
                                "https://api.example.qq.com/users/123456789/"
                                "store?access_token=QUERY_SENTINEL_9f3c"
                                "#FRAGMENT_SENTINEL_9f3c"
                            ),
                            "headers": [
                                {"name": "Authorization", "value": secrets[0]},
                                {"name": "Cookie", "value": secrets[1]},
                                {"name": "Content-Type", "value": "application/json"},
                            ],
                            "queryString": [
                                {"name": "access_token", "value": secrets[2]}
                            ],
                            "postData": {
                                "mimeType": "application/json",
                                "text": json.dumps(
                                    {
                                        "accountId": "123456789",
                                        "password": secrets[4],
                                        "nested": {"displayName": "private-name"},
                                    }
                                ),
                            },
                        },
                        "response": {
                            "status": 200,
                            "redirectURL": (
                                "https://example.qq.com/callback?ticket=" + secrets[6]
                            ),
                            "headers": [
                                {"name": "Set-Cookie", "value": secrets[5]},
                                {"name": "Location", "value": secrets[6]},
                            ],
                            "content": {
                                "mimeType": "application/json; charset=utf-8",
                                "text": json.dumps(
                                    {
                                        "items": [
                                            {
                                                "offerId": "item-public-looking",
                                                "price": 1775,
                                            }
                                        ],
                                        "refreshToken": secrets[5],
                                    }
                                ),
                            },
                        },
                    }
                ],
            }
        }

        result = sanitize_har(har)
        serialized = json.dumps(result, ensure_ascii=False)

        for secret in [*secrets, "WS_SENTINEL_9f3c", "private-name", "192.0.2.10"]:
            self.assertNotIn(secret, serialized)
        self.assertEqual(result["entry_count"], 1)
        entry = result["entries"][0]
        self.assertEqual(entry["request"]["method"], "POST")
        self.assertEqual(
            entry["request"]["endpoint"],
            "https://api.example.qq.com/users/<id>/store",
        )
        self.assertEqual(entry["request"]["query_names"], ["access_token"])
        self.assertIn("authorization", entry["request"]["header_names"])
        self.assertEqual(entry["request"]["body"]["schema"]["password"], "<removed>")
        self.assertEqual(
            entry["response"]["body"]["schema"]["refreshToken"], "<removed>"
        )
        self.assertEqual(
            entry["response"]["body"]["schema"]["items"]["type"], "array"
        )

    def test_non_json_and_base64_bodies_are_never_copied(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://example.com/upload",
                            "headers": [],
                            "postData": {
                                "mimeType": "multipart/form-data; boundary=private",
                                "text": "RAW_MULTIPART_SENTINEL",
                                "params": [
                                    {
                                        "name": "screenshot",
                                        "value": "RAW_FILE_SENTINEL",
                                        "fileName": "account-name.png",
                                    }
                                ],
                            },
                        },
                        "response": {
                            "status": 200,
                            "headers": [],
                            "content": {
                                "mimeType": "application/octet-stream",
                                "encoding": "base64",
                                "text": "BASE64_SENTINEL",
                            },
                        },
                    }
                ]
            }
        }

        serialized = json.dumps(sanitize_har(har))
        for secret in [
            "RAW_MULTIPART_SENTINEL",
            "RAW_FILE_SENTINEL",
            "account-name.png",
            "BASE64_SENTINEL",
            "private",
        ]:
            self.assertNotIn(secret, serialized)
        self.assertIn("<opaque-body-removed>", serialized)
        self.assertIn("screenshot", serialized)

    def test_rejects_non_har_documents(self):
        for document in [None, {}, {"log": {}}, {"log": {"entries": "bad"}}]:
            with self.assertRaises(ValueError):
                sanitize_har(document)

    def test_loader_accepts_utf8_bom(self):
        document = {"log": {"entries": []}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(document).encode("utf-8"))
            self.assertEqual(_load_har_document(path), document)

    def test_loader_identifies_common_wrong_formats(self):
        cases = {
            "empty.har": (b"", "empty"),
            "capture.pcapng": (b"\x0a\x0d\x0d\x0a" + b"binary", "PCAP"),
            "capture.saz": (b"PK\x03\x04" + b"binary", "ZIP/SAZ"),
            "capture.har.gz": (b"\x1f\x8b" + b"binary", "gzip"),
            "page.html": (b"<!doctype html><html></html>", "HTML/XML"),
            "capture.txt": (b"not a har", "plain text"),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, (content, expected) in cases.items():
                with self.subTest(name=name):
                    path = Path(directory) / name
                    path.write_bytes(content)
                    with self.assertRaisesRegex(ValueError, expected):
                        _load_har_document(path)


if __name__ == "__main__":
    unittest.main()
