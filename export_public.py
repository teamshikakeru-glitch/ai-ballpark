#!/usr/bin/env python3
"""
公開用ファイル一式を dist-public/ に書き出すスクリプト（OSS公開の準備用）。

ホワイトリスト方式：公開してよいと明示したファイル・ディレクトリだけをコピーする
（ディレクトリを丸ごとコピーする、ではない）。ローカル設定（agents.config.json /
mask.config.json / data.json / instructions/ など、実際の企業データや稼働ログを
含みうるファイル）は絶対にコピーしない。コピー後、禁止ファイル名の二重チェックも行う。

使い方:
    cd path/to/ai-ballpark
    python3 export_public.py
    # -> dist-public/ が生成される（既存の場合は作り直す）
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DIST_DIR = SCRIPT_DIR / "dist-public"

# 公開してよいファイル（ホワイトリスト）
PUBLIC_FILES = [
    "index.html",
    "serve_office.py",
    "generate_data.py",
    "README.md",
    "LICENSE",
    ".gitignore",
    "export_public.py",
]

# 公開してよいディレクトリ（ホワイトリスト。中身は全部公開してよいもののみ置くこと）
PUBLIC_DIRS = [
    "samples",
]

# 絶対に公開しないファイル/ディレクトリ名（ホワイトリストが主。これは万一の二重チェック）
FORBIDDEN_NAMES = {
    "agents.config.json",
    "mask.config.json",
    "data.json",
    "instructions",
    ".claude",
    "__pycache__",
    ".env",
}


def main() -> None:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)

    copied: list[str] = []

    for name in PUBLIC_FILES:
        src = SCRIPT_DIR / name
        if not src.is_file():
            print(f"[警告] {name} が見つからないためスキップします")
            continue
        dst = DIST_DIR / name
        shutil.copy2(src, dst)
        copied.append(name)

    for dname in PUBLIC_DIRS:
        src = SCRIPT_DIR / dname
        if not src.is_dir():
            print(f"[警告] {dname}/ が見つからないためスキップします")
            continue
        dst = DIST_DIR / dname
        shutil.copytree(src, dst)
        for p in sorted(dst.rglob("*")):
            if p.is_file():
                copied.append(str(p.relative_to(DIST_DIR)))

    # 二重チェック：禁止ファイルが誤って含まれていないか（ホワイトリスト運用の事故防止）
    leaked = [
        str(p.relative_to(DIST_DIR))
        for p in DIST_DIR.rglob("*")
        if p.name in FORBIDDEN_NAMES
    ]
    if leaked:
        print("[エラー] 公開禁止ファイルが dist-public/ に含まれています。書き出しを中止します:")
        for item in leaked:
            print("  -", item)
        shutil.rmtree(DIST_DIR)
        sys.exit(1)

    print(f"[OK] {DIST_DIR} に {len(copied)}件を書き出しました:")
    for c in sorted(copied):
        print("  -", c)
    print()
    print("次のステップ:")
    print("  1. dist-public/samples/agents.config.sample.json を")
    print("     dist-public/agents.config.json としてコピーし、repo_pathとエージェント定義を編集")
    print("  2. cd dist-public && python3 serve_office.py で動作確認")
    print("  3. 固有名詞スキャン（顧客名・社名等が残っていないか）を実施してから公開する")


if __name__ == "__main__":
    main()
