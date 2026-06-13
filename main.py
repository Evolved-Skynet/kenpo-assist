import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import licensing

load_dotenv()

IS_FROZEN = getattr(sys, "frozen", False)


def resource_path(rel: str) -> str:
    """同梱された読み取り専用リソース（staticファイル等）の絶対パスを返す。
    PyInstaller でexe化すると一時展開先 sys._MEIPASS に配置される。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / rel)


def data_dir() -> Path:
    """書き込み可能なデータ保存先（DB等）。exe化時はユーザーごとのホーム配下に置く。"""
    if IS_FROZEN:
        base = Path.home() / ".kenpo-support"
    else:
        base = Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


DB_PATH = str(data_dir() / "kenpo_support.db")
LICENSE_PATH = data_dir() / "license.key"


def current_license() -> dict:
    """有効化済みライセンスを読み、検証して payload を返す。無効/未有効化なら None。"""
    if not LICENSE_PATH.exists():
        return None
    try:
        return licensing.verify_license_string(LICENSE_PATH.read_text().strip())
    except Exception:
        return None


def require_license():
    """ライセンス必須エンドポイントのガード。"""
    if current_license() is None:
        raise HTTPException(status_code=403, detail="ライセンスが有効化されていません。製品ライセンスキーを入力してください。")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            category TEXT,
            content TEXT NOT NULL,
            ai_draft TEXT,
            ai_references TEXT DEFAULT '',
            final_response TEXT,
            status TEXT DEFAULT '未対応',
            staff TEXT,
            notes TEXT,
            chat_history TEXT DEFAULT '[]'
        )
    """)
    # 既存DBへのカラム追加（初回以降）
    for col_def in [
        "ALTER TABLE inquiries ADD COLUMN chat_history TEXT DEFAULT '[]'",
        "ALTER TABLE inquiries ADD COLUMN ai_references TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(col_def)
        except Exception:
            pass
    # アプリ設定（選択中のAIプロバイダ等）をローカル保存する
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = None) -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=resource_path("static")), name="static")


class InquiryCreate(BaseModel):
    category: str = ""
    content: str
    staff: str = ""
    provider: str = None


class SettingsUpdate(BaseModel):
    provider: str = None


class SetupRequest(BaseModel):
    provider: str


class LicenseActivate(BaseModel):
    key: str


class InquiryUpdate(BaseModel):
    final_response: str = None
    status: str = None
    staff: str = None
    notes: str = None
    category: str = None


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    inquiry_id: int
    inquiry_content: str
    category: str = ""
    ai_draft: str
    history: list[ChatMessage]
    question: str
    provider: str = None


class RefineRequest(BaseModel):
    inquiry_content: str
    category: str = ""
    ai_draft: str  # 元のAI生成草案（上書きせず再生成の土台にする）
    chat_answer: str  # 反映対象となるAI相談の回答
    history: list[ChatMessage] = []
    provider: str = None


@app.get("/")
def index():
    return FileResponse(resource_path("static/index.html"))


# ───────────────────────── ライセンス ─────────────────────────

@app.get("/api/license/status")
def license_status():
    lic = current_license()
    if lic is None:
        return {"licensed": False}
    return {
        "licensed": True,
        "name": lic.get("name"),
        "expires": lic.get("expires"),
        "seats": lic.get("seats"),
    }


@app.post("/api/license/activate")
def license_activate(data: LicenseActivate):
    try:
        payload = licensing.verify_license_string(data.key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    LICENSE_PATH.write_text(data.key.strip())
    return {"licensed": True, "name": payload.get("name"), "expires": payload.get("expires")}


@app.get("/api/settings")
def read_settings():
    """選択中プロバイダと、各CLIの導入状況（インストール有無）を返す"""
    current = resolve_provider()
    providers = [
        {
            "id": pid,
            "label": conf["label"],
            "available": shutil.which(conf["executable"]) is not None,
        }
        for pid, conf in PROVIDERS.items()
    ]
    return {"provider": current, "providers": providers}


@app.put("/api/settings")
def write_settings(data: SettingsUpdate):
    if data.provider is not None:
        if data.provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail="不明なAIプロバイダです")
        set_setting("provider", data.provider)
    return {"ok": True, "provider": resolve_provider()}


# ───────────────────────── 初期設定ウィザード ─────────────────────────
# 非エンジニアの利用者が、AIのCLI導入とログインを画面から行えるようにする。
#  - 導入(npm install)は自動化
#  - ログインはOAuthのためユーザー操作が必須 → ボタンで起動し、接続確認で検証

IS_WINDOWS = sys.platform.startswith("win")


def _node_version() -> str:
    node = shutil.which("node")
    if not node:
        return None
    try:
        r = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None


@app.get("/api/setup/status")
def setup_status():
    """Node.js/npm と各AI CLIの導入状況を返す（初期設定画面用）"""
    node_ver = _node_version()
    providers = [
        {
            "id": pid,
            "label": conf["label"],
            "executable": conf["executable"],
            "package": conf["package"],
            "installed": shutil.which(conf["executable"]) is not None,
        }
        for pid, conf in PROVIDERS.items()
    ]
    return {
        "node": {"installed": node_ver is not None, "version": node_ver},
        "npm": {"installed": shutil.which("npm") is not None},
        "providers": providers,
    }


@app.post("/api/setup/install")
def setup_install(data: SetupRequest):
    """選んだAIのCLIを npm で自動インストールする"""
    if data.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="不明なAIプロバイダです")
    if shutil.which("npm") is None:
        raise HTTPException(
            status_code=400,
            detail="Node.js（npm）が見つかりません。先に Node.js（https://nodejs.org/ のLTS版）をインストールしてください。",
        )
    conf = PROVIDERS[data.provider]
    package = conf["package"]
    try:
        if IS_WINDOWS:
            # Windowsのnpmはバッチ(.cmd)のためshell経由で実行
            result = subprocess.run(
                f"npm install -g {package}",
                shell=True, capture_output=True, text=True, timeout=600,
            )
        else:
            result = subprocess.run(
                [shutil.which("npm"), "install", "-g", package],
                capture_output=True, text=True, timeout=600,
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="インストールがタイムアウトしました。通信環境をご確認のうえ再度お試しください。")
    log = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    installed = shutil.which(conf["executable"]) is not None
    if result.returncode != 0 and not installed:
        raise HTTPException(status_code=500, detail=f"インストールに失敗しました。\n{log[-800:]}")
    return {"ok": True, "installed": installed, "log": log[-2000:]}


@app.post("/api/setup/login")
def setup_login(data: SetupRequest):
    """ログイン用CLIを新しいターミナルで起動する（OAuthはブラウザで完了してもらう）"""
    if data.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="不明なAIプロバイダです")
    conf = PROVIDERS[data.provider]
    if shutil.which(conf["executable"]) is None:
        raise HTTPException(status_code=400, detail=f"{conf['label']} のCLIが未導入です。先に「導入する」を実行してください。")
    login_cmd = conf["login_cmd"]
    cmd_str = " ".join(login_cmd)
    opened = False
    try:
        if IS_WINDOWS:
            subprocess.Popen(f'start "AIログイン" cmd /k {cmd_str}', shell=True)
            opened = True
        elif sys.platform == "darwin":
            subprocess.Popen([
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd_str}"',
                "-e", 'tell application "Terminal" to activate',
            ])
            opened = True
        else:
            # Linux: 代表的なターミナルを順に試す
            for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
                if shutil.which(term):
                    subprocess.Popen([term, "-e", "bash", "-lc", f"{cmd_str}; exec bash"])
                    opened = True
                    break
    except Exception:
        opened = False
    return {
        "opened": opened,
        "command": cmd_str,
        "message": (
            "ログイン用の画面を開きました。表示された案内（ブラウザでの許可など）に従ってログインを完了してください。"
            if opened else
            f"自動でターミナルを開けませんでした。お手数ですが、コマンドプロンプト等で「{cmd_str}」を実行してログインしてください。"
        ),
    }


@app.post("/api/setup/verify")
def setup_verify(data: SetupRequest):
    """実際に短いリクエストを送り、ログイン済みで応答できるかを確認する"""
    if data.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="不明なAIプロバイダです")
    conf = PROVIDERS[data.provider]
    if shutil.which(conf["executable"]) is None:
        return {"ok": False, "logged_in": False, "detail": f"{conf['label']} のCLIが未導入です。"}
    try:
        out = call_ai("1+1を半角数字だけで答えてください。", data.provider, timeout=90)
    except RuntimeError as e:
        return {"ok": False, "logged_in": False, "detail": str(e)}
    except Exception:
        return {"ok": False, "logged_in": False, "detail": "確認中にエラーが発生しました。"}
    return {"ok": True, "logged_in": bool(out.strip()), "detail": "接続できました。"}


SYSTEM_PROMPT = """あなたは健康保険組合の事務担当者をサポートするAIアシスタントです。
被保険者からの問い合わせに対する丁寧な回答文の下案と、その根拠となる参考文献を作成してください。

必ず以下の形式で出力してください（セクション見出しを含めること）：

【回答文】
（回答本文をここに記載）

【参考文献・根拠】
（根拠となる法令・通達・規則を箇条書きで記載）

【回答文】の規則：
- 書き出しは「お問い合わせいただきありがとうございます。」
- 具体的で分かりやすい説明
- 必要に応じて「詳しくは担当窓口までお問い合わせください」を末尾に追加
- 敬語・丁寧語を使用
- 300〜500字程度

【参考文献・根拠】の規則：
- 回答の根拠となった健康保険法・同施行規則・厚生労働省通達等を具体的に列挙する
- 条文が特定できる場合は条番号も記載する（例：健康保険法 第106条（任意継続被保険者））
- 複数ある場合は「・」で箇条書きにする
- 根拠が健保組合独自規程の場合は「健康保険組合規程による」と記載する"""


def parse_draft_response(response: str) -> tuple[str, str]:
    """AI出力を回答文本体と参考文献に分割する。
    一部のCLIは見出しの前に内部的な前置き（思考やツール出力）を混ぜることがあるため、
    【回答文】見出し以降のみを本文として採用し、前置きを除去する。"""
    refs = ""
    body = response
    if "【参考文献・根拠】" in body:
        body, refs = body.split("【参考文献・根拠】", 1)
    if "【回答文】" in body:
        # 見出し以降を採用 → 見出し前の前置き（例: gemini の update_topic{...}）を除去
        body = body.split("【回答文】", 1)[1]
    return body.strip(), refs.strip()


# 利用可能なAIプロバイダ。購入者が自分のサブスク（個人利用）でログイン済みの
# 公式CLIをローカルで呼び出す。各CLIの非対話実行コマンドは仕様変更があり得るため、
# ここを一箇所変更すれば全体に反映される。
PROVIDERS = {
    "claude": {
        "label": "Claude",
        "executable": "claude",
        "package": "@anthropic-ai/claude-code",  # npm install -g 対象
        "login_cmd": ["claude", "auth", "login"],  # 初期設定ウィザードのログイン起動
        # --safe-mode で周辺設定（CLAUDE.md・スキル・フック・MCP等）を無効化し、
        # 実行マシンの設定が回答に混入しないようにする（サブスク認証は維持される）。
        "build_cmd": lambda prompt: ["claude", "-p", "--safe-mode", prompt],
    },
    "chatgpt": {
        "label": "ChatGPT",
        "executable": "codex",
        "package": "@openai/codex",
        "login_cmd": ["codex", "login"],
        # --skip-git-repo-check: 中立な作業ディレクトリ（非gitリポジトリ）でも実行できるようにする
        "build_cmd": lambda prompt: ["codex", "exec", "--skip-git-repo-check", prompt],
    },
    "gemini": {
        "label": "Gemini",
        "executable": "gemini",
        "package": "@google/gemini-cli",
        "login_cmd": ["gemini"],  # 初回起動時に認証フローが開始される
        # --skip-trust: 中立な作業ディレクトリ（非信頼フォルダ）でも実行できるようにする
        "build_cmd": lambda prompt: ["gemini", "--skip-trust", "-p", prompt],
    },
}

DEFAULT_PROVIDER = os.getenv("DEFAULT_AI_PROVIDER", "claude")

AI_TIMEOUT = int(os.getenv("AI_TIMEOUT", "120"))

# CLIは作業ディレクトリの設定ファイル（CLAUDE.md / AGENTS.md / GEMINI.md 等）を
# 自動探索することがある。中立な空ディレクトリで実行し、周辺設定の混入を防ぐ。
AI_CWD = tempfile.mkdtemp(prefix="kenpo_ai_")


def resolve_provider(requested: str = None) -> str:
    """リクエスト指定 → 保存設定 → 既定 の順でプロバイダを決定する"""
    provider = requested or get_setting("provider", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    return provider


def call_ai(prompt: str, provider: str = None, timeout: int = None) -> str:
    provider = resolve_provider(provider)
    conf = PROVIDERS[provider]
    cmd = conf["build_cmd"](prompt)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout or AI_TIMEOUT, cwd=AI_CWD,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"{conf['label']} のCLI（{conf['executable']}）が見つかりません。"
            f"インストールと、ご自身のアカウントでのログインを確認してください。"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{conf['label']} の応答がタイムアウトしました。時間をおいて再度お試しください。")
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or "").strip()
            or f"{conf['label']} の実行に失敗しました。ログイン状態・利用上限をご確認ください。"
        )
    return result.stdout.strip()


@app.post("/api/inquiries")
def create_inquiry(data: InquiryCreate):
    require_license()
    prompt = f"{SYSTEM_PROMPT}\n\n【問い合わせカテゴリ】{data.category}\n\n【問い合わせ内容】\n{data.content}"
    try:
        raw = call_ai(prompt, data.provider)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="AI生成中にエラーが発生しました")

    ai_draft, ai_references = parse_draft_response(raw)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO inquiries (created_at, category, content, ai_draft, ai_references, staff) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), data.category, data.content, ai_draft, ai_references, data.staff)
    )
    inquiry_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {"id": inquiry_id, "ai_draft": ai_draft, "ai_references": ai_references}


CHAT_SYSTEM = """あなたは健康保険組合の事務担当者をサポートするAIアシスタントです。
以下の問い合わせと回答草案について、担当者からの質問や確認に答えてください。

回答の方針：
- 不明点・解釈の確認には具体的に答える
- 必要なら草案の修正案を提示する
- 健保法令・実務に沿った正確な情報を提供する
- 簡潔・丁寧に回答する"""


@app.post("/api/chat")
def chat(data: ChatRequest):
    require_license()
    history_text = ""
    for msg in data.history:
        role = "担当者" if msg.role == "user" else "AI"
        history_text += f"\n{role}: {msg.content}"

    prompt = f"""{CHAT_SYSTEM}

【問い合わせカテゴリ】{data.category}
【問い合わせ内容】
{data.inquiry_content}

【現在の回答草案】
{data.ai_draft}
{f"【これまでの会話】{history_text}" if history_text else ""}

【担当者からの質問】
{data.question}"""

    try:
        answer = call_ai(prompt, data.provider)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="AI応答中にエラーが発生しました")

    # チャット履歴をDBに保存
    import json
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT chat_history FROM inquiries WHERE id = ?", (data.inquiry_id,)).fetchone()
    existing = json.loads(row[0] or "[]") if row else []
    existing.append({"role": "user", "content": data.question})
    existing.append({"role": "assistant", "content": answer})
    conn.execute("UPDATE inquiries SET chat_history = ? WHERE id = ?",
                 (json.dumps(existing, ensure_ascii=False), data.inquiry_id))
    conn.commit()
    conn.close()

    return {"answer": answer}


REFINE_SYSTEM = """あなたは健康保険組合の事務担当者をサポートするAIアシスタントです。
担当者がAIと相談して得られた補足・修正方針を、元の回答草案に反映した「最終回答」を作成してください。

重要な方針：
- 元の回答草案の体裁・トーン・構成を土台として維持する
- AI相談で得られた指摘・修正・追記内容を草案に統合する
- 相談内容で草案を丸ごと置き換えるのではなく、必要な箇所だけを反映・加筆・修正する
- 健保法令・実務に沿った正確で丁寧な回答にする
- 書き出しは「お問い合わせいただきありがとうございます。」を維持する

出力は最終回答の本文のみとし、見出し記号（【】）や前置き・解説は付けないでください。"""


@app.post("/api/refine")
def refine(data: RefineRequest):
    require_license()
    history_text = ""
    for msg in data.history:
        role = "担当者" if msg.role == "user" else "AI"
        history_text += f"\n{role}: {msg.content}"

    prompt = f"""{REFINE_SYSTEM}

【問い合わせカテゴリ】{data.category}
【問い合わせ内容】
{data.inquiry_content}

【元のAI回答草案】
{data.ai_draft}
{f"【AIとの相談履歴】{history_text}" if history_text else ""}

【今回反映するAI相談の回答】
{data.chat_answer}

上記の相談内容を踏まえ、元のAI回答草案を土台に最終回答を再生成してください。"""

    try:
        answer = call_ai(prompt, data.provider)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="最終回答の再生成中にエラーが発生しました")

    return {"final_response": answer.strip()}


@app.get("/api/inquiries")
def list_inquiries(status: str = None, keyword: str = None, category: str = None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM inquiries WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if category:
        query += " AND category = ?"
        params.append(category)
    if keyword:
        query += " AND (content LIKE ? OR category LIKE ? OR notes LIKE ?)"
        params.extend([f"%{keyword}%"] * 3)

    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/inquiries/{inquiry_id}")
def get_inquiry(inquiry_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM inquiries WHERE id = ?", (inquiry_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="見つかりません")
    return dict(row)


@app.put("/api/inquiries/{inquiry_id}")
def update_inquiry(inquiry_id: int, data: InquiryUpdate):
    fields = {k: v for k, v in data.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="更新項目がありません")

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [inquiry_id]

    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"UPDATE inquiries SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/inquiries/{inquiry_id}")
def delete_inquiry(inquiry_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM inquiries WHERE id = ?", (inquiry_id,))
    conn.commit()
    conn.close()
    return {"ok": True}
