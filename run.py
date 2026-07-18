#!/usr/bin/env python3
"""ケンポアシスト（KenpoAssist）ランチャ

ローカルでサーバーを起動し、既定ブラウザで画面を自動的に開きます。
非エンジニアの利用者がダブルクリック相当で起動できることを想定しています。

    python run.py            # ソースから起動
    ./kenpo-support          # exe化した配布物から起動

環境変数 PORT で待受ポートを変更できます（既定: 8765）。
"""
import os
import threading
import webbrowser

import uvicorn

# exe化時もimport文字列ではなくappオブジェクトを直接渡すため、ここでimportする
from main import app

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8765"))


def _open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}/")


def main():
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️ 警告: HOST={HOST} が設定されています。このアプリを他のPCから見える")
        print("   アドレスで公開すると、問い合わせデータ（個人情報）が同じネットワークの")
        print("   第三者から閲覧できるおそれがあります。通常は HOST を設定しないでください。")
    # サーバー起動直後にブラウザを開く（起動待ちのため少し遅延）
    threading.Timer(1.5, _open_browser).start()
    print(f"ケンポアシストを起動します → http://{HOST}:{PORT}/")
    print("終了するには Ctrl+C を押してください。")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
