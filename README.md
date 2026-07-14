# AI Ballpark — バーチャルオフィス / Virtual Office for your AI agent team

*(English follows the Japanese section below.)*

あなたのAIエージェント組織（またはただの開発チーム）を、野球場テーマの動く2Dオフィスとして
可視化するローカルツールです。**吹き出し・数字・ターミナルのログは全て実データ（gitリポジトリの
コミット履歴）由来**。手書き・想像による捏造は一切行いません。

公開リポジトリ：[teamshikakeru-glitch/ai-ballpark](https://github.com/teamshikakeru-glitch/ai-ballpark)

## 何が違うのか / Differentiators

- **gitリポジトリの実活動データ駆動**：Claude Codeセッションの監視ではなく `git log` を直接読むため、
  どんな言語・どんなツールで書かれたリポジトリでも動きます（Claude Codeを使っていなくても動作します）
- **野球場テーマ**：9つの守備位置（投手・捕手・一塁〜遊撃・外野）にエージェントを配置し、
  稼働中は守備位置周辺を、待機中はゆっくり動き回ります
- **監督室チャット（指示キュー）**：チームへの指示を書くと `instructions/queue.jsonl` に実際に
  追記されます。AIの自動応答を試みますが、失敗時も嘘の返信は作らず正直に失敗を表示します
- **config駆動**：エージェントの人数（1〜12人）・名前・役割・色・守備位置・コミット分類ルールは
  全て設定ファイル（JSON）で決まります。画面から「+ エージェント」ボタンで追加、詳細パネルから削除もできます

## 3分で動かす / Quickstart

```bash
git clone https://github.com/teamshikakeru-glitch/ai-ballpark.git
cd ai-ballpark
cp samples/agents.config.sample.json agents.config.json
cp samples/mask.config.sample.json mask.config.json   # 任意（マスクが要らなければ省略可）
# agents.config.json を開き、repo_path を可視化したい自分のgitリポジトリのパスに、
# agents の中身を自分のチーム構成に書き換える
python3 serve_office.py
# -> http://localhost:8793 をブラウザで開く
```

Python標準ライブラリのみで動作します（追加インストール不要）。外部CDN依存もありません。

## ファイル構成

```
index.html          ビューア本体（単一HTMLファイル。外部CDN依存なし）
generate_data.py     agents.config.json を読み、gitリポジトリをスキャンして data.json を生成する
serve_office.py      ローカルAPIサーバー（データ配信・ターミナル・チャット・エージェント追加削除）
agents.config.json   あなたのチーム構成（gitignore対象。samples/からコピーして作る）
mask.config.json     個人名・顧客名のマスク辞書（任意。無ければマスクなしで動く）
samples/             サンプル設定一式（架空5人チーム）とサンプルdata.json
export_public.py     このツール自体を配布用に書き出すスクリプト（フォークして自作したい人向け）
```

## 画面構成

- **TODAY'S SCORE**（左上）：守備中ポジション数・直近7日コミット・本日のコミット・直近の成果物
- **PLAY-BY-PLAY**（右上）：直近コミットの横スクロールティッカー
- **フィールド**：9守備位置にエージェントを配置。稼働中は自席で作業→数十秒ごとに守備範囲内を移動、
  待機中はゆっくり徘徊。クリックで役割・直近の仕事・直近コミットの詳細パネルを開ける
- **ベンチレポート**（右）：稼働中エージェントの活動フィード、または実ログのターミナル表示に切替可能
- **監督室**（右下）：エージェントへの指示を送信。指示は実ファイルに記録される
- **+ エージェント**：フォームでエージェントを追加（ID・名前・役割・色・守備位置・分類ルール）。
  詳細パネルから削除も可能

## 設定ファイル仕様（agents.config.json）

```json
{
  "repo_path": "../..",
  "team_name": "MY AI BALLPARK",
  "subtitle": "5 AGENTS · ONE TEAM",
  "home_label": "HOME",
  "away_label": "AWAY",
  "agents": [
    {
      "id": "coder",
      "name": "Coder",
      "role": "実装・バグ修正を担当する",
      "color": "#69aa99",
      "position": "SS",
      "is_fallback": false,
      "match": {
        "scope_tokens": ["coder"],
        "subject_keywords": [],
        "path_prefixes": ["src/"]
      }
    }
  ]
}
```

- `repo_path`：可視化したいgitリポジトリへのパス（`agents.config.json`からの相対パス、または絶対パス）
- `position`：`P/C/1B/2B/3B/SS/LF/CF/RF` のいずれか、または省略（ベンチ扱い）。1〜12人まで対応
  （9人を超えた分・位置が重複した分は自動的にベンチへ回る）
- `is_fallback`：どの分類ルールにも一致しなかったコミットの受け皿にするエージェントに `true` を1つ指定
  （省略時は先頭のエージェントが受け皿になる）
- `match.scope_tokens`：コミット件名が `種別(スコープ): 本文` 形式のとき、スコープ部分の完全一致
- `match.subject_keywords`：スコープで判定できない場合の、件名全体に対する部分一致フォールバック
- `match.path_prefixes`：変更ファイルパスの前方一致（例: `"src/"`）

## セキュリティ

- `.env` ・シークレットファイルには一切アクセスしません
- 実行コマンドは全てホワイトリスト固定・リスト引数・`shell=False`。ユーザー入力をシェルへ渡す経路はありません
- `agents.config.json` への書き込みはJSONとしてのみ扱い、`path_prefixes` はファイルパスの結合には使わず
  文字列の前方一致にのみ使います（絶対パス・`..`を含む値はAPI側で拒否）
- サーバーは `127.0.0.1` のみでバインドします（ネットワークに公開されません）
- 静的ファイル配信もホワイトリスト方式です

## 既知の制限

- ローカル専用ツールです（本番公開・複数人での同時利用は想定していません）
- 監督室チャットの「claude CLIによる自動応答」は環境依存です。`claude` CLIがインストールされ、
  かつそのサブプロセスに有効な認証情報が引き継がれる環境でのみ動作します。動かない場合も
  指示の記録（`instructions/queue.jsonl`への追記）自体は成功し、UIには正直に失敗理由が表示されます
- `data.json` は30分ごとに自動再生成されますが、それより高頻度の更新が必要な場合は
  `generate_data.py` を手動実行するか、cron等で定期実行してください
- モバイル幅（縦長画面）でのレイアウト最適化は行っていません（デスクトップでの利用を想定）

## ライセンス

MIT License. `LICENSE` ファイルを参照してください。

---

# AI Ballpark — Virtual Office for your AI agent team

A local tool that visualizes your AI agent team (or just a regular dev team) as a baseball-themed
2D office that actually moves. **Every speech bubble, every number, every terminal log line comes
from real data** — your git repository's commit history. Nothing is fabricated or hand-written.

Repository: [teamshikakeru-glitch/ai-ballpark](https://github.com/teamshikakeru-glitch/ai-ballpark)

## What makes it different

- **Driven by real git activity, not session monitoring**: it reads `git log` directly instead of
  watching Claude Code sessions, so it works with any repository regardless of language or tooling
  (you don't even need to be using Claude Code)
- **Baseball theme**: agents are placed at 9 fielding positions (pitcher, catcher, infield, outfield).
  Active agents work at their position and occasionally move around their fielding range; idle agents
  wander slowly
- **Manager's office chat (instruction queue)**: write an instruction to the team and it's really
  appended to `instructions/queue.jsonl`. It attempts an AI auto-reply but never fabricates one on
  failure — failures are shown honestly in the UI
- **Config-driven**: the number of agents (1–12), their names, roles, colors, fielding positions, and
  commit-classification rules are all defined in a JSON config file. Add agents from the UI with a
  "+ Agent" button, remove them from the detail panel

## 3-minute quickstart

```bash
git clone https://github.com/teamshikakeru-glitch/ai-ballpark.git
cd ai-ballpark
cp samples/agents.config.sample.json agents.config.json
cp samples/mask.config.sample.json mask.config.json   # optional, skip if you don't need masking
# Edit agents.config.json: set repo_path to the git repository you want to visualize,
# and edit the agents array to match your own team
python3 serve_office.py
# -> open http://localhost:8793 in your browser
```

Runs on the Python standard library only — no extra installs, no external CDN dependencies.

## File layout

```
index.html          The viewer itself (single HTML file, no external CDN)
generate_data.py     Reads agents.config.json, scans the git repo, produces data.json
serve_office.py      Local API server (data serving, terminal, chat, agent add/remove)
agents.config.json   Your team configuration (gitignored — copy from samples/ to create)
mask.config.json     Name-masking dictionary (optional; runs unmasked if absent)
samples/             Sample configs (a fictional 5-person team) and a sample data.json
export_public.py     Script to export a distributable copy of this tool (for forking/rebranding)
```

## Screen layout

- **TODAY'S SCORE** (top-left): active positions, commits in the last 7 days, commits today, latest artifact
- **PLAY-BY-PLAY** (top-right): a scrolling ticker of recent commits
- **The field**: agents placed at 9 fielding positions. Active agents work at their desk and
  occasionally move within their fielding range every few dozen seconds; idle agents wander slowly.
  Click any agent to see their role, recent work, and recent commits
- **Bench report** (right rail): an activity feed of active agents, toggleable to a real-log terminal view
- **Manager's office** (bottom-right): send instructions to the team; instructions are recorded to a real file
- **+ Agent**: add an agent via a form (ID, name, role, color, fielding position, classification rules).
  Remove agents from the detail panel

## Config file reference (agents.config.json)

See the Japanese section above for the schema — it's the same JSON either way. Key fields:

- `repo_path`: path to the git repository to visualize (relative to `agents.config.json`, or absolute)
- `position`: one of `P/C/1B/2B/3B/SS/LF/CF/RF`, or omitted for bench. Supports 1–12 agents
  (anyone beyond 9, or with a duplicate position, automatically goes to the bench)
- `is_fallback`: set `true` on exactly one agent to catch commits that don't match any rule
  (defaults to the first agent if unset)
- `match.scope_tokens`: exact match against the `(scope)` portion of `type(scope): subject` commit messages
- `match.subject_keywords`: substring fallback against the full subject when scope matching fails
- `match.path_prefixes`: prefix match against changed file paths (e.g. `"src/"`)

## Security

- Never touches `.env` or secret files
- All executed commands are fixed whitelist, list-args, `shell=False` — no path for user input to reach a shell
- Writes to `agents.config.json` are handled purely as JSON; `path_prefixes` are only ever used for
  string prefix matching, never for filesystem path joins (absolute paths and `..` are rejected by the API)
- The server binds to `127.0.0.1` only (never exposed to the network)
- Static file serving is whitelist-only

## Known limitations

- This is a local-only tool (not designed for production hosting or multi-user concurrent use)
- The manager's-office chat's "claude CLI auto-reply" is environment-dependent. It only works if the
  `claude` CLI is installed and its subprocess inherits valid credentials. When it doesn't work, the
  instruction is still recorded successfully (appended to `instructions/queue.jsonl`), and the UI
  honestly displays the failure reason instead of fabricating a reply
- `data.json` auto-regenerates every 30 minutes; if you need more frequent updates, run
  `generate_data.py` manually or schedule it with cron
- No mobile (portrait) layout optimization — designed for desktop use

## License

MIT License — see the `LICENSE` file.
