"""
Server-side inspection of inline (``data:``) file/signature payloads.

The browser sends files as ``{"name", "size", "type", "url": "data:<mime>;base64,..."}``.
Nothing the client claims about size or type is trusted: the size is recomputed
from the base64 payload, the MIME type is taken from the data URL, and the first
bytes are checked against the file's extension for common formats.
"""

from __future__ import annotations

import base64
import re
from typing import Optional

from . import codes
from .limits import MAX_FILE_URL_LENGTH

_B64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_URL_RE = re.compile(r"https?://[^\s\x00-\x1f\x7f]+", re.IGNORECASE)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".png": (PNG_MAGIC,),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".pdf": (b"%PDF",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".webp": (b"RIFF",),
}


class PayloadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def inspect_data_url(url: str, *, max_bytes: int, code: str = codes.INVALID_FILE_PAYLOAD) -> tuple[str, int, bytes]:
    """Return ``(mime, decoded_size, first_bytes)`` or raise ``PayloadError``."""
    header, sep, payload = url.partition(",")
    if not sep or not header.lower().startswith("data:") or not header.lower().endswith(";base64"):
        raise PayloadError(code, "Embedded content must be a base64 data URL.")
    mime = header[5:].split(";", 1)[0].strip().lower()
    if not mime or "/" not in mime:
        raise PayloadError(code, "Embedded content has no valid MIME type.")
    if len(payload) > (max_bytes * 4) // 3 + 8:
        raise PayloadError(codes.FILE_TOO_LARGE, f"Embedded content exceeds the {max_bytes // (1024 * 1024) or 1} MB limit.")
    if len(payload) % 4 != 0 or not _B64_RE.fullmatch(payload):
        raise PayloadError(code, "Embedded content is not valid base64.")
    padding = len(payload) - len(payload.rstrip("="))
    size = (len(payload) // 4) * 3 - padding
    if size > max_bytes:
        raise PayloadError(codes.FILE_TOO_LARGE, f"Embedded content exceeds the {max_bytes // (1024 * 1024) or 1} MB limit.")
    head = base64.b64decode(payload[:32]) if payload else b""
    return mime, size, head


def check_plain_url(url: str, code: str = codes.INVALID_FILE_PAYLOAD) -> str:
    if len(url) > MAX_FILE_URL_LENGTH:
        raise PayloadError(code, "File link is too long.")
    if not _URL_RE.fullmatch(url):
        raise PayloadError(code, "File link must be an http(s) URL.")
    return url


def extension(name: str) -> str:
    return "." + name.rsplit(".", 1)[1].lower() if "." in name else ""


def check_magic(name: str, head: bytes, size: int) -> Optional[str]:
    """Return an error message when the leading bytes contradict the extension."""
    expected = _MAGIC.get(extension(name))
    if not expected or size == 0:
        return None
    if not any(head.startswith(m) for m in expected):
        return f"'{name}' does not look like a real {extension(name)} file."
    return None
