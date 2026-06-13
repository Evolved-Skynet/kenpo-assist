"""オフライン署名ライセンスの検証モジュール（製品側）。

ライセンスキーの形式:  base64url(payload_json) + "." + base64url(signature)
  payload_json 例: {"expires":"2027-03-31","issued":"2026-06-13","name":"○○健保組合","seats":1}
  署名は販売者の Ed25519 秘密鍵で payload_json のバイト列に対して作成され、
  ここに埋め込んだ公開鍵で検証する。秘密鍵は配布物に含めない（issue_license.py 側で保管）。
"""
import base64
import json
from datetime import date

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

# 販売者の公開鍵（issue_license.py --genkeys で生成した公開鍵を貼り付ける）
PUBLIC_KEY_B64 = "e8yOfaa8USCxHuWVFy66mAznL8wMpD9wUpEI6wviN7E="


def _public_key() -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))


def verify_license_string(key: str) -> dict:
    """ライセンスキーを検証し payload(dict) を返す。無効な場合 ValueError を送出。"""
    try:
        payload_b64, sig_b64 = key.strip().split(".", 1)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        signature = base64.urlsafe_b64decode(sig_b64)
    except Exception:
        raise ValueError("ライセンスキーの形式が正しくありません。")

    try:
        _public_key().verify(signature, payload_bytes)
    except InvalidSignature:
        raise ValueError("ライセンスキーが正規のものではありません。")

    try:
        payload = json.loads(payload_bytes)
    except Exception:
        raise ValueError("ライセンスキーの内容を読み取れません。")

    expires = payload.get("expires")
    if expires and date.today().isoformat() > expires:
        raise ValueError(f"ライセンスの有効期限が切れています（{expires}）。更新版のキーをお求めください。")

    return payload
