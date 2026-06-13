#!/usr/bin/env python3
"""ライセンスキー発行ツール（販売者専用・配布物には含めない）。

初回のみ鍵ペアを生成し、公開鍵を licensing.py の PUBLIC_KEY_B64 に貼り付けます。
秘密鍵ファイル（license_private_key.txt）は販売者だけが厳重に保管してください。

使い方:
  # 1. 鍵ペアの生成（初回のみ）
  python issue_license.py --genkeys

  # 2. ライセンスキーの発行（販売のたびに実行）
  python issue_license.py --name "○○健康保険組合" --expires 2027-03-31 --seats 1
  python issue_license.py --name "△△健保" --seats 1        # 無期限（expires省略）
"""
import argparse
import base64
import json
import sys
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PRIVATE_KEY_FILE = Path(__file__).resolve().parent / "license_private_key.txt"


def genkeys():
    if PRIVATE_KEY_FILE.exists():
        print(f"既に秘密鍵が存在します: {PRIVATE_KEY_FILE}")
        print("上書きすると発行済みキーが全て無効になります。中止しました。")
        sys.exit(1)
    priv = Ed25519PrivateKey.generate()
    PRIVATE_KEY_FILE.write_text(base64.b64encode(priv.private_bytes_raw()).decode())
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    print("鍵ペアを生成しました。")
    print(f"  秘密鍵: {PRIVATE_KEY_FILE} （厳重に保管。絶対に配布・公開しないこと）")
    print()
    print("以下の公開鍵を licensing.py の PUBLIC_KEY_B64 に貼り付けてください:")
    print(f'  PUBLIC_KEY_B64 = "{pub_b64}"')


def issue(name: str, expires: str, seats: int):
    if not PRIVATE_KEY_FILE.exists():
        print("秘密鍵がありません。先に `python issue_license.py --genkeys` を実行してください。")
        sys.exit(1)
    raw = base64.b64decode(PRIVATE_KEY_FILE.read_text().strip())
    priv = Ed25519PrivateKey.from_private_bytes(raw)

    payload = {
        "name": name,
        "issued": date.today().isoformat(),
        "expires": expires,  # None なら無期限
        "seats": seats,
    }
    # 検証側は送られてきたバイト列をそのまま検証するため、ここでの整形が唯一の正本となる
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    signature = priv.sign(payload_bytes)

    key = (
        base64.urlsafe_b64encode(payload_bytes).decode()
        + "."
        + base64.urlsafe_b64encode(signature).decode()
    )
    print("発行したライセンスキー（購入者にお渡しください）:")
    print()
    print(key)


def main():
    p = argparse.ArgumentParser(description="ライセンスキー発行ツール（販売者専用）")
    p.add_argument("--genkeys", action="store_true", help="鍵ペアを生成（初回のみ）")
    p.add_argument("--name", help="購入者名（健保組合名など）")
    p.add_argument("--expires", default=None, help="有効期限 YYYY-MM-DD（省略で無期限）")
    p.add_argument("--seats", type=int, default=1, help="席数（既定: 1）")
    args = p.parse_args()

    if args.genkeys:
        genkeys()
    elif args.name:
        issue(args.name, args.expires, args.seats)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
