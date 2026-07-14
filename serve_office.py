#!/usr/bin/env python3
"""
バーチャルオフィス（AI BALLPARK） - ローカルAPIサーバー

静的ファイル配信（index.html / data.json）に加え、以下の実データAPIを提供する。

  GET    /api/terminal   本物のログだけを返す（git log / リポジトリ直近更新ファイル /
                          任意の観測ログディレクトリの直近ファイル冒頭）。演出用の偽コマンド・
                          偽出力は一切含まない。
  POST   /api/instruct   監督室チャットの指示を instructions/queue.jsonl に追記する。
                          claude CLI（claude -p）によるヘッドレス応答をベストエフォートで試み、
                          失敗・未インストール時はエラー内容をそのまま返す（偽の返信は作らない）。
  GET    /api/agents     agents.config.json の現在のエージェント一覧を返す。
  POST   /api/agents     エージェントを1人追加する（+エージェントボタン）。
  DELETE /api/agents/{id} エージェントを1人削除する。

GET /data.json は配信前に鮮度を確認し、generated_at が30分より古ければ
generate_data.py を自動で再実行してから返す。ロックで直列化し多重実行はしない。
/api/agents での追加・削除の直後も、同じ仕組みでdata.jsonを即時再生成する。

設定ファイルはこのスクリプトと同じディレクトリの agents.config.json / mask.config.json を既定で使う
（環境変数 AGENTS_CONFIG / MASK_CONFIG で上書き可能）。エージェント定義・マスク辞書に
特定企業のデータは一切含まれない（config駆動の一本化：社内版と公開版は同じコードで動く）。

セキュリティ設計：
  - .env・.secrets 等のシークレットファイルには一切アクセスしない
  - 実行するコマンドは全てホワイトリスト固定。ユーザー入力をシェルへ渡さない
    （subprocess はリスト引数・shell=False のみ。os.system/シェル文字列は使わない）
  - agents.config.json への書き込みはJSONとしてのみ扱う。エージェントが指定する
    path_prefixes は「文字列としての前方一致」にのみ使い、ファイルパス結合には使わない
    （絶対パス・".."を含む値はAPI側でも拒否し、二重に防御する）
  - 顧客・個人名のマスクは generate_data.load_mask_map() / make_mask_fn() をそのまま再利用する
  - バインドは 127.0.0.1 のみ（ネットワークに公開しない）
  - 書き込み系APIは同一ローカルオリジンからのリクエストのみ受け付ける
  - 静的配信もホワイトリスト方式（ディレクトリ丸ごと公開はしない）

Python標準ライブラリのみで完結。CDN・外部パッケージ依存なし。

使い方:
    cd /path/to/ai-ballpark-office
    python3 serve_office.py
    # -> http://localhost:8793 で待ち受け（Ctrl+Cで終了）

    ポート・設定ファイルを変えたい場合:
    PORT=9000 AGENTS_CONFIG=/path/to/agents.config.json python3 serve_office.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# generate_data.py を同ディレクトリからモジュールとして再利用する
# （設定読み込み・マスク関数・守備位置テーブルを二重管理しないためのDRY）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_data  # noqa: E402

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8793"))
BIND_HOST = "127.0.0.1"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

STATIC_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AGENTS_CONFIG", str(STATIC_DIR / "agents.config.json")))
MASK_CONFIG_PATH = Path(os.environ.get("MASK_CONFIG", str(STATIC_DIR / "mask.config.json")))
DATA_JSON_PATH = STATIC_DIR / "data.json"

# 静的配信はホワイトリスト方式。ディレクトリを丸ごと公開しない
ALLOWED_STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/data.json": "data.json",
    "/README.md": "README.md",
}
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}

INSTRUCTIONS_DIR = STATIC_DIR / "instructions"
QUEUE_PATH = INSTRUCTIONS_DIR / "queue.jsonl"
QUEUE_LOCK = threading.Lock()

TERMINAL_CACHE_TTL_SEC = 3
_terminal_cache: dict = {"ts": 0.0, "payload": None}
_terminal_lock = threading.Lock()

MAX_WALK_FILES = 20000  # 巨大ディレクトリでの暴走防止（安全弁）

# data.jsonの鮮度切れ（前夜生成のまま翌朝古い数字が出る）対策。
# generated_at がこの秒数より古ければ自動再生成する。ロックで直列化し多重実行しない。
DATA_JSON_MAX_AGE_SEC = 30 * 60  # 30分
DATA_JSON_REGEN_TIMEOUT_SEC = 60
_data_regen_lock = threading.Lock()

CLAUDE_TIMEOUT_SEC = 75
CLAUDE_MAX_REPLY_CHARS = 800
CLAUDE_MAX_PROMPT_CONTEXT_CHARS = 2000

# /api/agents 入力検証の上限値
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_AGENTS = 12
MAX_NAME_LEN = 40
MAX_ROLE_LEN = 300
MAX_MATCH_ITEMS = 20
MAX_MATCH_ITEM_LEN = 60
_config_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 設定ファイルの読み書き（generate_data.pyの読み込みロジックを共有する）
# ---------------------------------------------------------------------------


def _load_config_snapshot() -> tuple[dict, Path, "callable"]:
    """現在のconfig・repo_root・mask関数を読み込む（毎回ファイルから読む＝常に最新）。"""
    config = generate_data.load_agents_config(CONFIG_PATH)
    repo_root = generate_data.resolve_repo_root(config, CONFIG_PATH)
    mask_map = generate_data.load_mask_map(MASK_CONFIG_PATH)
    mask = generate_data.make_mask_fn(mask_map)
    return config, repo_root, mask


def _read_config_raw() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config_raw(config: dict) -> None:
    """agents.config.json をアトミックに書き換える（tmpファイル書き込み→rename）。"""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def _run_generate_data() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                sys.executable, str(STATIC_DIR / "generate_data.py"),
                "--config", str(CONFIG_PATH),
                "--mask-config", str(MASK_CONFIG_PATH),
                "--output", str(DATA_JSON_PATH),
            ],
            cwd=str(STATIC_DIR),
            capture_output=True,
            text=True,
            timeout=DATA_JSON_REGEN_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            return False, (detail[0] if detail else f"exit={result.returncode}")
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{e.__class__.__name__}: {e}"


def _regenerate_data_json_now() -> None:
    """設定変更直後などに、鮮度に関わらず即座にdata.jsonを再生成する。"""
    with _data_regen_lock:
        ok, err = _run_generate_data()
        if not ok:
            sys.stderr.write(f"[serve_office] data.json再生成に失敗しました: {err}\n")


# ---------------------------------------------------------------------------
# /api/terminal 用：実コマンド・実ファイルスキャン（ホワイトリスト固定）
# ---------------------------------------------------------------------------


def _run_git_log(repo_root: Path, mask, n: int = 20) -> list[str]:
    """git log の実出力をそのまま返す（マスク適用込み）。ユーザー入力は一切介在しない。"""
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_root), "log", f"-{n}",
                "--pretty=format:%h %ad %s", "--date=format:%Y-%m-%d %H:%M",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        lines = result.stdout.splitlines()
    except Exception as e:  # noqa: BLE001 - ここは実行失敗を握りつぶさず可視化する
        return [f"(git log 実行エラー: {e.__class__.__name__})"]
    return [mask(line) for line in lines]


def _recent_files(repo_root: Path, mask, max_files: int = 10) -> list[str]:
    """リポジトリ全体の直近更新ファイルをos.walkで収集する（shellのfind等は使わない）。

    2026-07-14 OSS公開対応：以前は「部署ディレクトリ」固定リストの配下だけを走査していたが、
    config駆動化に伴い任意のリポジトリ構成に対応する必要があるため、repo_root全体を
    （安全弁MAX_WALK_FILES付きで）走査する方式にした。ユーザー指定のpath_prefixesを
    ファイルシステムの結合に使うことは無い（文字列の前方一致にしか使わない）ため、
    ここでのパストラバーサルの懸念自体が構造的に存在しない。
    """
    candidates: list[tuple[float, Path]] = []
    scanned = 0
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv", ".venv", "dist-public")
        ]
        for fn in files:
            if fn.startswith(".env") or fn.startswith("."):
                continue
            scanned += 1
            if scanned > MAX_WALK_FILES:
                break
            p = Path(root) / fn
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, p))
        if scanned > MAX_WALK_FILES:
            break

    candidates.sort(key=lambda t: t[0], reverse=True)
    lines = []
    for mtime, p in candidates[:max_files]:
        rel = p.relative_to(repo_root)
        ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(mask(f"{ts}  {rel}"))
    return lines


def build_terminal_payload() -> dict:
    """短いTTLでキャッシュしつつ、実コマンド結果を組み立てる。"""
    now = time.time()
    with _terminal_lock:
        cached = _terminal_cache["payload"]
        if cached is not None and (now - _terminal_cache["ts"]) < TERMINAL_CACHE_TTL_SEC:
            return cached

        _config, repo_root, mask = _load_config_snapshot()

        lines: list[str] = []
        lines.append("$ git log --oneline -20")
        lines.extend(_run_git_log(repo_root, mask, 20))
        lines.append("")
        lines.append("$ リポジトリ 直近更新ファイル (mtime desc)")
        lines.extend(_recent_files(repo_root, mask, 10))

        payload = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "lines": lines,
        }
        _terminal_cache["ts"] = now
        _terminal_cache["payload"] = payload
        return payload


# ---------------------------------------------------------------------------
# /data.json 用：鮮度切れ時の自動再生成
# ---------------------------------------------------------------------------


def _data_json_generated_at() -> "datetime | None":
    try:
        obj = json.loads(DATA_JSON_PATH.read_text(encoding="utf-8"))
        return datetime.fromisoformat(obj["generated_at"])
    except Exception:  # noqa: BLE001 - 壊れたJSON・欠損キーも含めて「古い」扱いにする
        return None


def _data_json_is_stale() -> bool:
    if not DATA_JSON_PATH.is_file():
        return True
    generated_at = _data_json_generated_at()
    if generated_at is None:
        return True
    now = datetime.now().astimezone()
    age_sec = (now - generated_at).total_seconds()
    return age_sec > DATA_JSON_MAX_AGE_SEC


def ensure_data_json_fresh() -> None:
    """data.jsonが古ければ generate_data.py を再実行して更新する（single-flightロック）。"""
    if not _data_json_is_stale():
        return
    with _data_regen_lock:
        if not _data_json_is_stale():
            return  # ロック待ちの間に他のリクエストが既に更新済み
        ok, err = _run_generate_data()
        if not ok:
            sys.stderr.write(f"[serve_office] data.json自動再生成に失敗しました: {err}\n")


# ---------------------------------------------------------------------------
# /api/instruct 用：queue.jsonl 追記 ＋ claude -p ベストエフォート応答
# ---------------------------------------------------------------------------


def _find_claude_cli() -> "str | None":
    return shutil.which("claude")


def _try_claude_headless(dept_id: str, dept_name: str, role: str, text: str) -> tuple:
    """claude -p によるヘッドレス応答をベストエフォートで試みる。偽の返信は絶対に作らない。

    既知の制限：この開発環境で検証したところ、`claude -p` を新規サブプロセスとして
    起動すると認証エラー（401 Invalid authentication credentials）で確実に失敗する
    （このサーバーを動かしているセッション自体の認証情報が、新規プロセスには
    引き継がれないため）。失敗時は例外を握りつぶさず、理由をそのまま返す。
    """
    claude_path = _find_claude_cli()
    if not claude_path:
        return None, "claude CLIが見つかりません（未インストール）"

    prompt = (
        f"あなたは{dept_name}です。役割: {role}\n\n"
        f"指示: {text}\n\n"
        "この指示に対する短い一次応答（3行以内）だけを返してください。"
    )

    try:
        result = subprocess.run(
            [claude_path, "-p"],
            cwd=str(STATIC_DIR),
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
            input=prompt,
        )
    except subprocess.TimeoutExpired:
        return None, f"claude -p が{CLAUDE_TIMEOUT_SEC}秒でタイムアウトしました"
    except Exception as e:  # noqa: BLE001
        return None, f"claude -p 実行エラー: {e.__class__.__name__}"

    if result.returncode != 0:
        detail_source = (result.stdout or "").strip() or (result.stderr or "").strip()
        detail_lines = detail_source.splitlines()
        detail = detail_lines[0][:200] if detail_lines else f"exit={result.returncode}"
        return None, "claude -p が失敗しました: " + detail

    _config, _repo_root, mask = _load_config_snapshot()
    reply = mask(result.stdout.strip())
    if not reply:
        return None, "claude -p の応答が空でした"
    return reply[:CLAUDE_MAX_REPLY_CHARS], None


def append_instruction(dept_id: str, dept_name: str, text: str) -> dict:
    INSTRUCTIONS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "dept": dept_id,
        "dept_name": dept_name,
        "text": text,
    }
    with QUEUE_LOCK:
        with QUEUE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# ---------------------------------------------------------------------------
# /api/agents 用：+エージェント追加 / 削除（入力検証はここに集約）
# ---------------------------------------------------------------------------


def _validate_prefix(p: str) -> bool:
    """path_prefixesは文字列の前方一致にしか使わないが、念のため二重に防御する
    （絶対パス・".."を含む値はリポジトリ外を意図した入力とみなして拒否する）。"""
    if not isinstance(p, str) or not p or len(p) > MAX_MATCH_ITEM_LEN:
        return False
    if p.startswith("/") or ".." in p:
        return False
    return True


def _validate_agent_payload(payload: dict, existing_ids: set) -> tuple:
    if not isinstance(payload, dict):
        return None, "リクエストボディが不正です"

    aid = str(payload.get("id", "")).strip()
    name = str(payload.get("name", "")).strip()
    role = str(payload.get("role", "")).strip()
    color = str(payload.get("color", "#8899aa")).strip()
    position = payload.get("position")
    match = payload.get("match") or {}

    if not AGENT_ID_RE.match(aid):
        return None, "id は半角小文字英数字と-_のみ、1〜32文字で指定してください"
    if aid in existing_ids:
        return None, f"id '{aid}' は既に使われています"
    if not name or len(name) > MAX_NAME_LEN:
        return None, f"name は1〜{MAX_NAME_LEN}文字で指定してください"
    if len(role) > MAX_ROLE_LEN:
        return None, f"role は{MAX_ROLE_LEN}文字以内にしてください"
    if not HEX_COLOR_RE.match(color):
        return None, "color は #rrggbb 形式（例: #5ba8c9）で指定してください"
    valid_positions = set(generate_data.POSITION_LAYOUT.keys())
    if position not in (None, "") and position not in valid_positions:
        return None, "position は " + ",".join(sorted(valid_positions)) + " のいずれか、または空欄（ベンチ）にしてください"
    if not isinstance(match, dict):
        return None, "match はオブジェクトで指定してください"

    cleaned_match: dict = {}
    for key in ("scope_tokens", "subject_keywords", "path_prefixes"):
        items = match.get(key, [])
        if not isinstance(items, list) or len(items) > MAX_MATCH_ITEMS:
            return None, f"match.{key} は{MAX_MATCH_ITEMS}件以内のリストで指定してください"
        cleaned: list = []
        for it in items:
            if not isinstance(it, str) or not it or len(it) > MAX_MATCH_ITEM_LEN:
                return None, f"match.{key} の各項目は1〜{MAX_MATCH_ITEM_LEN}文字の文字列にしてください"
            if key == "path_prefixes" and not _validate_prefix(it):
                return None, "path_prefixes に絶対パス('/'始まり)や'..'は使えません"
            cleaned.append(it)
        cleaned_match[key] = cleaned

    agent = {
        "id": aid,
        "name": name,
        "role": role,
        "color": color,
        "position": position or None,
        "match": cleaned_match,
    }
    return agent, None


def _public_agent_view(a: dict) -> dict:
    return {
        "id": a["id"], "name": a["name"], "role": a.get("role", ""),
        "color": a.get("color", "#8899aa"), "position": a.get("position"),
        "is_fallback": bool(a.get("is_fallback")),
        "match": a.get("match", {}),
    }


# ---------------------------------------------------------------------------
# HTTPハンドラ
# ---------------------------------------------------------------------------


class OfficeHandler(BaseHTTPRequestHandler):
    server_version = "AIBallparkOffice/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        sys.stderr.write("[serve_office] " + (fmt % args) + "\n")

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_name: str) -> None:
        path = STATIC_DIR / rel_name
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = STATIC_CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _is_allowed_origin(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in ALLOWED_HOSTS:
            return False

        for header_name in ("Origin", "Referer"):
            value = self.headers.get(header_name)
            if not value:
                continue
            parsed = urlparse(value)
            origin_host = (parsed.netloc or "").lower()
            if parsed.scheme not in ("http", "https") or origin_host not in ALLOWED_HOSTS:
                return False
        return True

    def _read_json_body(self, max_len: int = 20000) -> "tuple[dict | None, str | None]":
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > max_len:
            return None, "invalid request size"
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "invalid json"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/terminal":
                self._send_json(build_terminal_payload())
                return
            if path == "/api/agents":
                config = _read_config_raw()
                self._send_json({"agents": [_public_agent_view(a) for a in config.get("agents", [])]})
                return
            if path in ALLOWED_STATIC_FILES:
                rel_name = ALLOWED_STATIC_FILES[path]
                if rel_name == "data.json":
                    ensure_data_json_fresh()
                self._send_static(rel_name)
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[serve_office] GET {path} failed: {e!r}\n")
            self._send_json({"error": "internal error"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if not self._is_allowed_origin():
                self._send_json({"error": "forbidden origin"}, 403)
                return
            if path == "/api/instruct":
                self._handle_instruct()
                return
            if path == "/api/agents":
                self._handle_agents_post()
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[serve_office] POST {path} failed: {e!r}\n")
            self._send_json({"error": "internal error"}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if not self._is_allowed_origin():
                self._send_json({"error": "forbidden origin"}, 403)
                return
            m = re.match(r"^/api/agents/([a-z][a-z0-9_-]{0,31})$", path)
            if m:
                self._handle_agent_delete(m.group(1))
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[serve_office] DELETE {path} failed: {e!r}\n")
            self._send_json({"error": "internal error"}, 500)

    def _handle_instruct(self) -> None:
        payload, err = self._read_json_body()
        if err:
            self._send_json({"error": err}, 400)
            return

        config = _read_config_raw()
        agents = config.get("agents", [])
        by_id = {a["id"]: a for a in agents}
        fallback = next((a for a in agents if a.get("is_fallback")), agents[0] if agents else None)

        dept_id = str(payload.get("dept", "")).strip()
        text = str(payload.get("text", "")).strip()
        agent = by_id.get(dept_id) or fallback
        if agent is None:
            self._send_json({"error": "エージェントが設定されていません"}, 400)
            return
        if not text:
            self._send_json({"error": "text is required"}, 400)
            return
        text = text[:1000]

        record = append_instruction(agent["id"], agent["name"], text)

        response = {
            "received": True,
            "queued_at": record["timestamp"],
            "dept_name": record["dept_name"],
            "message": "指示を受領しました。次回巡回で処理します。",
            "claude_attempted": False,
            "claude_reply": None,
            "claude_error": None,
        }

        if _find_claude_cli():
            response["claude_attempted"] = True
            reply, error = _try_claude_headless(agent["id"], agent["name"], agent.get("role", ""), text)
            response["claude_reply"] = reply
            response["claude_error"] = error

        self._send_json(response)

    def _handle_agents_post(self) -> None:
        payload, err = self._read_json_body()
        if err:
            self._send_json({"error": err}, 400)
            return

        with _config_lock:
            config = _read_config_raw()
            agents = config.get("agents", [])
            if len(agents) >= MAX_AGENTS:
                self._send_json({"error": f"エージェントは最大{MAX_AGENTS}人までです"}, 400)
                return
            existing_ids = {a["id"] for a in agents}
            agent, verr = _validate_agent_payload(payload, existing_ids)
            if verr:
                self._send_json({"error": verr}, 400)
                return
            agents.append(agent)
            config["agents"] = agents
            _write_config_raw(config)

        _regenerate_data_json_now()
        self._send_json({"ok": True, "agent": _public_agent_view(agent)})

    def _handle_agent_delete(self, agent_id: str) -> None:
        with _config_lock:
            config = _read_config_raw()
            agents = config.get("agents", [])
            if len(agents) <= 1:
                self._send_json({"error": "最後の1人は削除できません"}, 400)
                return
            idx = next((i for i, a in enumerate(agents) if a["id"] == agent_id), None)
            if idx is None:
                self._send_json({"error": "指定されたエージェントが見つかりません"}, 404)
                return
            removed = agents.pop(idx)
            if removed.get("is_fallback") and agents:
                agents[0]["is_fallback"] = True
            config["agents"] = agents
            _write_config_raw(config)

        _regenerate_data_json_now()
        self._send_json({"ok": True, "removed_id": agent_id})


def main() -> None:
    INSTRUCTIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not QUEUE_PATH.exists():
        QUEUE_PATH.touch()

    if not CONFIG_PATH.is_file():
        print(
            f"[serve_office] 設定ファイルが見つかりません: {CONFIG_PATH}\n"
            f"                agents.config.sample.json をコピーして作成してください。"
        )
        sys.exit(1)

    server = ThreadingHTTPServer((BIND_HOST, PORT), OfficeHandler)
    print(f"[serve_office] http://{BIND_HOST}:{PORT} で待ち受け中（設定: {CONFIG_PATH.name} / Ctrl+Cで終了）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
