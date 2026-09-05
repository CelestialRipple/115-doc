"""短时、用途和资源绑定的操作凭据，与 STRM 播放密钥隔离。"""

import hashlib
import hmac
import json
from time import time


def sign_action(secret: str, purpose: str, resource_id: str, ttl: int = 3600) -> str:
    if not secret:
        raise ValueError("操作签名密钥尚未初始化")
    expiry = str(int(time()) + ttl)
    payload = json.dumps([purpose, resource_id, expiry], separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"{expiry}.{digest}"


def verify_action(secret: str, token: str, purpose: str, resource_id: str) -> bool:
    if not secret or not isinstance(token, str):
        return False
    expiry, separator, digest = token.partition(".")
    if not separator or not expiry.isdigit() or len(expiry) > 12 or len(digest) != 64:
        return False
    if int(expiry) < time():
        return False
    payload = json.dumps([purpose, resource_id, expiry], separators=(",", ":")).encode()
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)
