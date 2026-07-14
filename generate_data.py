#!/usr/bin/env python3
"""
バーチャルオフィス（AI Ballpark） - データ生成スクリプト

設定ファイル（既定: agents.config.json）で指定された任意のgitリポジトリの
git logを実データソースとしてスキャンし、data.json を生成する。
吹き出し・稼働状況・成果物は全て実ログ由来であり、手書き・想像による捏造は行わない。

エージェント定義（名前・役割・色・守備位置・分類ルール）は全て agents.config.json から読む。
このスクリプト自体には特定企業・特定チームに依存するデータは一切含まれない
（config駆動：使う人のチーム構成がそのままagents.config.jsonになる）。

シークレット・.env には一切触れない。個人名・顧客名のマスクは任意の mask.config.json から読む
（無ければマスクなしで動作する）。

使い方:
    cd /path/to/ai-ballpark-office
    python3 generate_data.py
    # -> agents.config.json を読み、data.json を更新する

    設定ファイルを指定したい場合:
    python3 generate_data.py --config /path/to/agents.config.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "agents.config.json"
DEFAULT_MASK_CONFIG_PATH = SCRIPT_DIR / "mask.config.json"
OUTPUT_PATH = SCRIPT_DIR / "data.json"

WINDOW_DAYS = 7
ACTIVE_WINDOW_HOURS = 24
MAX_BUBBLES_PER_AGENT = 4
MAX_RECENT_ARTIFACTS = 8
MAX_RECENT_COMMITS_PER_AGENT = 5
MAX_SUBJECT_LEN = 60
MAX_COMMIT_SUBJECT_LEN = 90

EXCLUDE_PATH_PREFIXES = (".env", ".secrets", "supabase/.env")

# 野球守備位置の座標テーブル（フィールド上の %位置）とポジション名。
# これは「野球場テーマ」という製品デザインそのものの一部であり、
# 特定企業に依存しないドメイン知識のため、agents.config.json 側ではなく
# ここに定数として持つ（config側は「どのポジションに就くか」のコードだけ指定する）。
POSITION_LAYOUT: dict[str, dict] = {
    "P":  {"name": "投手", "left": 50, "top": 70.2},
    "C":  {"name": "捕手", "left": 50, "top": 94},
    "1B": {"name": "一塁", "left": 66, "top": 66.7},
    "2B": {"name": "二塁", "left": 57, "top": 57.5},
    "3B": {"name": "三塁", "left": 34, "top": 66.7},
    "SS": {"name": "遊撃", "left": 43, "top": 57.5},
    "LF": {"name": "左翼", "left": 23, "top": 34.2},
    "CF": {"name": "中堅", "left": 50, "top": 20.8},
    "RF": {"name": "右翼", "left": 77, "top": 34.2},
}
# ベンチ（9守備位置に収まらない、または position 未指定/不正なエージェント）の並び座標
BENCH_LEFT_START = 8
BENCH_LEFT_STEP = 9
BENCH_TOP = 88


# ---------------------------------------------------------------------------
# 設定ファイルの読み込み
# ---------------------------------------------------------------------------


def load_agents_config(config_path: Path) -> dict:
    if not config_path.is_file():
        raise SystemExit(
            f"[エラー] 設定ファイルが見つかりません: {config_path}\n"
            f"        agents.config.sample.json を agents.config.json にコピーして、"
            f"repo_path とエージェント定義を編集してください。"
        )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[エラー] {config_path} のJSONが不正です: {e}") from e

    agents = config.get("agents")
    if not isinstance(agents, list) or len(agents) == 0:
        raise SystemExit(f"[エラー] {config_path} に agents（1件以上の配列）がありません")
    if len(agents) > 12:
        raise SystemExit(f"[エラー] agents は最大12人までです（{len(agents)}人が指定されています）")

    seen_ids = set()
    for a in agents:
        if "id" not in a or "name" not in a:
            raise SystemExit(f"[エラー] エージェント定義には id と name が必須です: {a}")
        if a["id"] in seen_ids:
            raise SystemExit(f"[エラー] エージェントidが重複しています: {a['id']}")
        seen_ids.add(a["id"])
        a.setdefault("role", "")
        a.setdefault("color", "#8899aa")
        a.setdefault("position", None)
        a.setdefault("icon", "")  # 既定は空（絵文字なし）。設定すれば表示される
        a.setdefault("match", {})
        a["match"].setdefault("scope_tokens", [])
        a["match"].setdefault("subject_keywords", [])
        a["match"].setdefault("path_prefixes", [])

    if not any(a.get("is_fallback") for a in agents):
        agents[0]["is_fallback"] = True  # 明示指定が無ければ先頭のエージェントを受け皿にする

    config.setdefault("team_name", "AI BALLPARK")
    config.setdefault("subtitle", str(len(agents)) + " AGENTS · ONE TEAM")
    config.setdefault("home_label", "HOME")
    config.setdefault("away_label", "AI TEAM")
    config.setdefault("scoreboard_label", config["team_name"])
    config.setdefault("repo_path", ".")
    return config


def resolve_repo_root(config: dict, config_path: Path) -> Path:
    repo_path = Path(config["repo_path"])
    if not repo_path.is_absolute():
        repo_path = (config_path.parent / repo_path).resolve()
    if not repo_path.is_dir():
        raise SystemExit(f"[エラー] repo_path が存在しません: {repo_path}")
    return repo_path


def load_mask_map(mask_config_path: Path) -> dict[str, str]:
    """個人名・顧客名のマスク辞書を読む。ファイルが無ければ空辞書（マスクなし）で動く。"""
    if not mask_config_path.is_file():
        return {}
    try:
        obj = json.loads(mask_config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[警告] {mask_config_path} のJSONが不正なため、マスクなしで続行します: {e}\n")
        return {}
    mask_map = obj.get("mask_map", {})
    if not isinstance(mask_map, dict):
        return {}
    return {str(k): str(v) for k, v in mask_map.items()}


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def make_mask_fn(mask_map: dict[str, str]):
    def mask(text: str) -> str:
        """個人名・顧客名を機械的にマスクする。想像で書き換えない。"""
        for real, fake in mask_map.items():
            text = text.replace(real, fake)
        return text
    return mask


def truncate(text: str, n: int = MAX_SUBJECT_LEN) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def run_git(args: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# git log 解析
# ---------------------------------------------------------------------------

US = "\x1f"  # unit separator（コミットメッセージ中に出現しない前提の区切り文字）
COMMIT_MARK = "@@COMMIT@@"


def fetch_commits(repo_root: Path) -> list[dict]:
    """git log 全履歴を取得し、コミットごとに {hash, date, subject, files} を返す。"""
    raw = run_git(
        [
            "log",
            f"--pretty=format:{COMMIT_MARK}%H{US}%ad{US}%s",
            "--date=iso-strict",
            "--name-only",
        ],
        repo_root,
    )

    commits: list[dict] = []
    current: Optional[dict] = None
    for line in raw.splitlines():
        if line.startswith(COMMIT_MARK):
            if current is not None:
                commits.append(current)
            body = line[len(COMMIT_MARK):]
            parts = body.split(US)
            commit_hash = parts[0] if len(parts) > 0 else ""
            date_str = parts[1] if len(parts) > 1 else ""
            subject = parts[2] if len(parts) > 2 else ""
            current = {
                "hash": commit_hash,
                "date": date_str,
                "subject": subject,
                "files": [],
            }
        elif line.strip() and current is not None:
            if not line.startswith(EXCLUDE_PATH_PREFIXES):
                current["files"].append(line.strip())
    if current is not None:
        commits.append(current)
    return commits


SUBJECT_SCOPE_RE = re.compile(r"^[^\(\)]*\(([^)]+)\):\s*(.*)$")
SCOPE_SPLIT_RE = re.compile(r"[・,/、]")


def classify_agent_ids(subject: str, files: list[str], agents: list[dict], fallback_id: str) -> set[str]:
    """コミットが関与するエージェントIDの集合を実データ（件名スコープ＋変更ファイル）から機械的に判定する。"""
    ids: set[str] = set()

    m = SUBJECT_SCOPE_RE.match(subject)
    if m:
        scope_raw = m.group(1)
        tokens = [t.strip() for t in SCOPE_SPLIT_RE.split(scope_raw)]
        for agent in agents:
            for token in tokens:
                if token in agent["match"]["scope_tokens"]:
                    ids.add(agent["id"])

    if not ids:
        for agent in agents:
            for kw in agent["match"]["subject_keywords"]:
                if kw in subject:
                    ids.add(agent["id"])

    for f in files:
        for agent in agents:
            for prefix in agent["match"]["path_prefixes"]:
                if f.startswith(prefix):
                    ids.add(agent["id"])

    if not ids:
        ids.add(fallback_id)

    return ids


def bubble_text(subject: str, mask) -> str:
    m = SUBJECT_SCOPE_RE.match(subject)
    body = m.group(2) if m and m.group(2) else subject
    return truncate(mask(body))


def commit_subject_masked(subject: str, mask) -> str:
    """クリック詳細パネルのコミット一覧用：件名全体（スコープ込み）をマスクして表示する。"""
    return truncate(mask(subject), MAX_COMMIT_SUBJECT_LEN)


# ---------------------------------------------------------------------------
# 守備位置の割り当て（config指定 + 破綻しないベンチ処理）
# ---------------------------------------------------------------------------


def assign_positions(agents: list[dict]) -> dict[str, dict]:
    """各エージェントに野球の守備位置座標を割り当てる。
    指定が無い/不正/重複であふれた分は自動的にベンチへ回す（1〜12人まで破綻しない）。
    """
    assigned: dict[str, dict] = {}
    used_codes: set[str] = set()
    bench_agents: list[dict] = []

    for a in agents:
        code = a.get("position")
        if code in POSITION_LAYOUT and code not in used_codes:
            used_codes.add(code)
            layout = POSITION_LAYOUT[code]
            assigned[a["id"]] = {
                "position": code,
                "position_name": layout["name"],
                "left": layout["left"],
                "top": layout["top"],
                "is_bench": False,
            }
        else:
            bench_agents.append(a)

    n_bench = len(bench_agents)
    for i, a in enumerate(bench_agents):
        # ベンチは横一列に均等配置（人数に応じて自動で間隔調整）
        span = BENCH_LEFT_STEP * max(n_bench - 1, 1)
        start = 50 - span / 2 if n_bench > 1 else 50
        left = start + BENCH_LEFT_STEP * i if n_bench > 1 else 50
        assigned[a["id"]] = {
            "position": "BENCH",
            "position_name": "ベンチ",
            "left": round(left, 1),
            "top": BENCH_TOP,
            "is_bench": True,
        }

    # 投手(P)が「中央・main」の扱い。Pが不在（ベンチ回り等）の場合は先頭エージェントを中央扱いにする
    center_id = next((a["id"] for a in agents if assigned.get(a["id"], {}).get("position") == "P"), agents[0]["id"])
    for aid, info in assigned.items():
        info["is_center"] = (aid == center_id)

    return assigned


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="バーチャルオフィスのdata.jsonを生成する")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help="agents.config.json のパス（既定: 同ディレクトリのagents.config.json）",
    )
    parser.add_argument(
        "--mask-config", type=Path, default=DEFAULT_MASK_CONFIG_PATH,
        help="mask.config.json のパス（既定: 同ディレクトリのmask.config.json。無くてもよい）",
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_PATH,
        help="出力先data.jsonのパス（既定: 同ディレクトリのdata.json）",
    )
    args = parser.parse_args()

    config = load_agents_config(args.config)
    repo_root = resolve_repo_root(config, args.config)
    mask_map = load_mask_map(args.mask_config)
    mask = make_mask_fn(mask_map)

    agent_defs = config["agents"]
    fallback_id = next(a["id"] for a in agent_defs if a.get("is_fallback"))
    position_map = assign_positions(agent_defs)

    now = datetime.now().astimezone()
    window_start = now - timedelta(days=WINDOW_DAYS)
    active_cutoff = now - timedelta(hours=ACTIVE_WINDOW_HOURS)
    # 「本日のコミット」というUIラベルに対しては、カレンダー日付（当日0時以降）で集計する
    # （直近24時間のローリングウィンドウとは意図的に区別する。稼働判定にはローリング窓を使う）
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    commits = fetch_commits(repo_root)

    for c in commits:
        try:
            c["dt"] = datetime.fromisoformat(c["date"])
        except ValueError:
            c["dt"] = None
    commits = [c for c in commits if c["dt"] is not None]
    commits.sort(key=lambda c: c["dt"], reverse=True)

    for c in commits:
        c["agent_ids"] = classify_agent_ids(c["subject"], c["files"], agent_defs, fallback_id)
        c["bubble"] = bubble_text(c["subject"], mask)

    commits_7d = [c for c in commits if c["dt"] >= window_start]
    commits_24h = [c for c in commits if c["dt"] >= active_cutoff]  # 稼働判定（active/idle）専用
    commits_today_calendar = [c for c in commits if c["dt"] >= today_start]  # 「本日のコミット」表示専用

    agents_out = []
    for a in agent_defs:
        aid = a["id"]
        dept_commits_all = [c for c in commits if aid in c["agent_ids"]]
        dept_commits_7d = [c for c in commits_7d if aid in c["agent_ids"]]

        last_active_dt = dept_commits_all[0]["dt"] if dept_commits_all else None
        status = "active" if (last_active_dt and last_active_dt >= active_cutoff) else "idle"

        bubbles = []
        seen = set()
        for c in dept_commits_7d:
            if c["bubble"] not in seen:
                bubbles.append(c["bubble"])
                seen.add(c["bubble"])
            if len(bubbles) >= MAX_BUBBLES_PER_AGENT:
                break

        recent_commits = [
            {
                "hash": c["hash"][:7],
                "date": c["dt"].strftime("%Y-%m-%d %H:%M"),
                "subject": commit_subject_masked(c["subject"], mask),
            }
            for c in dept_commits_all[:MAX_RECENT_COMMITS_PER_AGENT]
        ]

        pos = position_map[aid]
        agents_out.append(
            {
                "id": aid,
                "name": a["name"],
                "role": a["role"],
                "color": a["color"],
                "icon": a["icon"],
                "position": pos["position"],
                "position_name": pos["position_name"],
                "field_left": pos["left"],
                "field_top": pos["top"],
                "is_center": pos["is_center"],
                "is_bench": pos["is_bench"],
                "status": status,
                "last_active": last_active_dt.isoformat() if last_active_dt else None,
                "commits_7d": len(dept_commits_7d),
                "bubbles": bubbles,
                "recent_commits": recent_commits,
            }
        )

    active_count = sum(1 for a in agents_out if a["status"] == "active")

    recent_artifacts = []
    seen_subjects = set()
    id_to_name = {a["id"]: a["name"] for a in agents_out}
    for c in commits[:30]:
        if c["bubble"] in seen_subjects:
            continue
        seen_subjects.add(c["bubble"])
        # 代表となるエージェント名（複数関与時はconfig順で先頭のもの）
        rep_id = next((a["id"] for a in agent_defs if a["id"] in c["agent_ids"]), fallback_id)
        recent_artifacts.append(
            {
                "dept": id_to_name[rep_id],
                "text": c["bubble"],
                "date": c["dt"].strftime("%Y-%m-%d"),
            }
        )
        if len(recent_artifacts) >= MAX_RECENT_ARTIFACTS:
            break

    data = {
        "generated_at": now.isoformat(),
        "window_days": WINDOW_DAYS,
        "team_name": config["team_name"],
        "subtitle": config["subtitle"],
        "home_label": config["home_label"],
        "away_label": config["away_label"],
        "scoreboard_label": config["scoreboard_label"],
        "stats": {
            "active_departments": active_count,
            "total_departments": len(agents_out),
            "commits_7d": len(commits_7d),
            "commits_today": len(commits_today_calendar),
            "commits_24h": len(commits_24h),
            "latest_artifact": recent_artifacts[0]["text"] if recent_artifacts else "",
        },
        "agents": agents_out,
        "recent_artifacts": recent_artifacts,
    }

    args.output.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"[OK] {args.output} を更新しました"
        f"（設定: {args.config.name} / リポジトリ: {repo_root} / "
        f"コミット {len(commits)}件を走査、直近7日 {len(commits_7d)}件）"
    )


if __name__ == "__main__":
    main()
