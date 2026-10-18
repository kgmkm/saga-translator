"""作品ディレクトリのレイアウト正本（唯一の定義）。

**このファイルが「どこに何を置くか」の正本**。SKILL.md や references/*.md に
木を書き写さない（写すと必ずズレる）。パスを確認したいときは:

    python3 scripts/paths.py            # 一覧
    python3 scripts/paths.py --md       # md 用の木
    python3 scripts/saga.py paths       # 同上（エージェント向け）

lifetime の意味:
    keep      — 完成後も残る（purge の対象外）
    work      — 作業中のみ（assemble の purge で消える）
    transient — 実行中だけ現れる（消えても支障なし）

writer は主な書き手（複数ある場合は代表）。readers は省略可。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: lifetime の許容値（テストで検査する）
LIFETIMES = ("keep", "work", "transient")

#: kind の許容値
KINDS = ("file", "dir")


@dataclass(frozen=True)
class LayoutPath:
    id: str
    path: str          # <work>/ 相対。chNNN / bN / <dst> / <work_id> はパターン
    kind: str          # file / dir
    lifetime: str      # keep / work / transient
    writer: str        # 主な書き手（スクリプト名）
    note: str = ""


#: ツールが読み書きするパス（これ以外にツール管理のファイルは無い）
LAYOUT: tuple[LayoutPath, ...] = (
    # ── 作品ルート ────────────────────────────────────────────────
    LayoutPath("manifest_toml", "manifest.toml", "file", "keep", "ingest",
               "作品メタ（lang / dst / スキル版・コミット）"),
    LayoutPath("manifest_stem", "manifest", "file", "keep", "ingest",
               "拡張子を問わない読み込み口（load_structured）。.toml を優先"),
    LayoutPath("chapters_meta_toml", "chapters_meta.toml", "file", "keep", "split_chapters",
               "章・ブロックの対応表。全ツールの入口"),
    LayoutPath("chapters_meta_stem", "chapters_meta", "file", "keep", "split_chapters",
               "拡張子を問わない読み込み口（load_structured）"),
    LayoutPath("chapters_paras", "chapters/chNNN.paras.toml", "file", "keep", "split_chapters",
               "段落原文。purge 後も残す（commit --revise の受け皿）"),
    LayoutPath("originals_source", "originals/source.md", "file", "work", "ingest",
               "取り込んだ原文（正規化済み）"),
    LayoutPath("translation_input", "translation_input/{ch}_{b}_{mode}.md", "file", "work",
               "prepare", "prepare が組み立てたコンテキスト。LLM はこれだけ読む"),

    # ── 分析 ──────────────────────────────────────────────────────
    LayoutPath("analysis_characters", "analysis/characters-[ネタバレあり].toml", "file",
               "keep", "analyze-commit", "登場人物表（proposed は承認待ちの訳語変更）"),
    LayoutPath("analysis_terms", "analysis/terms.toml", "file", "keep", "analyze-commit",
               "用語集（訳語・ルビ・memo）"),
    LayoutPath("analysis_plot", "analysis/plot-[ネタバレあり].toml", "file", "keep",
               "analyze-commit", "あらすじ（prepare が予算内で注入）"),
    LayoutPath("analysis_titles", "analysis/titles.toml", "file", "keep", "agent",
               "作品題・章題。assemble は無いと止まる（--allow-src-title で回避）"),
    LayoutPath("analysis_age_report_md", "analysis/age_policy_report-[ネタバレあり].md", "file",
               "keep", "spicy age_policy", "spicy: 年齢・学年の確認票（人間向け）"),
    LayoutPath("analysis_age_report_toml", "analysis/age_policy_report-[ネタバレあり].toml",
               "file", "keep", "spicy age_policy",
               "spicy: 検出記録（status が未裁定件数を数える。.md と対で作られる）"),

    # ── ハウススタイル ────────────────────────────────────────────
    LayoutPath("house_style", "_house/house_style.toml", "file", "keep", "house_style init",
               "命名・敬称・記号などのハウススタイル。この場所だけが読まれる"),

    # ── 作業用（purge 対象外） ────────────────────────────────────
    LayoutPath("work_lock_prep_md", "_work/lock_prep.md", "file", "keep", "lock_prep",
               "人名・用語の候補一覧（判断材料。機械は読まない）"),
    LayoutPath("work_lock_prep_toml", "_work/lock_prep.toml", "file", "keep", "lock_prep",
               "lock_prep の機械用の候補（prepare が読む）"),
    LayoutPath("work_ledger", "_work/ledger.toml", "file", "keep", "commit",
               "台帳（ルビ初出・用語初出・使用済みオノマトペ）。連作は carry で引き継ぐ"),
    LayoutPath("work_rulings", "_work/rulings.json", "file", "keep", "rulings",
               "裁定記録（語彙 candidates / 年齢方針）"),
    LayoutPath("work_renames", "_work/renames.jsonl", "file", "keep", "rename_terms",
               "改名の履歴（1行1件）"),
    LayoutPath("work_analysis_edits", "_work/analysis_edits.jsonl", "file", "keep",
               "analyze-commit", "分析 TOML の訂正監査（保護キーの上書き・proposed の承認/却下）"),
    LayoutPath("work_review", "_work/review_waveN.md", "file", "keep", "review",
               "検収リスト。assemble はこの有無を見ない（検収は人力）"),
    LayoutPath("work_notes", "_work/notes.md", "file", "keep", "agent",
               "任意の判断メモ（機械は読まない。purge されない）"),
    LayoutPath("work_lock", "_work/.lock", "file", "transient", "lock",
               "工程排他。stale は所有者の生存確認つきでのみ上書き"),
    LayoutPath("work_analysis_backup", "_work/analysis_backup/", "dir", "keep",
               "analyze-commit", "分析 TOML の書き込み失敗時の退避"),
    LayoutPath("work_delta", "_work/{ch}_{b}_delta.toml", "file", "keep", "agent",
               "分析差分の入力（analyze-commit が読む。--delta で指定。analysis/ に置かない）"),

    # ── ログ ──────────────────────────────────────────────────────
    LayoutPath("logs_timeline", "logs/timeline.jsonl", "file", "keep", "timed_stage",
               "工程の所要時間・終了コード（1行1工程）"),
    LayoutPath("logs_last_context", "logs/last_context.json", "file", "keep", "prepare",
               "最後に prepare したコンテキスト（再開位置の手がかり）"),
    LayoutPath("logs_rename_last", "logs/rename_last.json", "file", "keep", "rename_terms",
               "最後の改名の対象・件数"),

    # ── 成果物 ────────────────────────────────────────────────────
    LayoutPath("output_full_ja", "output/全文_訳文.md", "file", "keep", "assemble",
               "全文（訳文）。assemble の主成果物"),
    LayoutPath("output_full_parallel", "output/全文_対訳.md", "file", "keep", "assemble",
               "全文の対訳（> [Pnnn] 付き）。--no-parallel で抑制"),
    LayoutPath("output_toc", "output/目次.md", "file", "keep", "assemble", "目次"),
    LayoutPath("output_chapter", "output/chNNN_<dst>.md", "file", "keep", "assemble",
               "章ごとの訳文"),
    LayoutPath("output_chapter_parallel", "output/chNNN_parallel.md", "file", "keep",
               "assemble", "章ごとの対訳"),
    LayoutPath("output_images", "output/images/", "dir", "keep", "ingest",
               "画像。訳文からは常に images/… で参照する"),
    LayoutPath("output_block", "output/chNNN/{b}_<dst>.md", "file", "work", "commit",
               "ブロック訳。単章作品だけ purge 後も残る（復元の受け皿）"),
    LayoutPath("output_block_parallel", "output/chNNN/{b}_parallel.md", "file", "work",
               "commit", "ブロック対訳の中間物"),
    LayoutPath("output_work", "output/_work/", "dir", "work", "commit",
               "確定前のドラフトの置き場（_work/ ではない）。purge で done/commit 以外を削除"),
    LayoutPath("output_work_done", "output/_work/done/", "dir", "keep", "commit",
               "確定ドラフトの退避（再検証・復元用）"),
    LayoutPath("output_work_commit", "output/_work/commit/", "dir", "keep", "commit",
               "監査記録（commit の報告・段落対応スナップショット）"),
    LayoutPath("output_work_draft_parts", "output/_work/draft_parts/", "dir", "transient",
               "draft-append", "長文ドラフトの追記パーツ"),
)

#: <work>/ の外側（入力フォルダ側）
OUTER: tuple[LayoutPath, ...] = (
    LayoutPath("outer_saga", "_saga/", "dir", "keep", "ingest",
               "入力フォルダ側の作品置き場（<入力フォルダ>/_saga/<作品ID>/）"),
    LayoutPath("house_style_parent", "_saga/_house/house_style.toml", "file", "keep",
               "house_style init", "シリーズ共通のハウススタイル。work 側の _house が優先"),
)

#: 旧配置。読めるが、新規に作らない（移行は各ツールが行う）
LEGACY: tuple[LayoutPath, ...] = (
    LayoutPath("legacy_manifest_json", "manifest.json", "file", "keep", "ingest",
               "旧形式。新規は manifest.toml"),
    LayoutPath("legacy_chapters_meta_json", "chapters_meta.json", "file", "keep",
               "split_chapters", "旧形式。新規は chapters_meta.toml"),
    LayoutPath("legacy_characters_toml", "analysis/characters.toml", "file", "keep",
               "analyze-commit", "ネタバレ表記なしの旧名。読み込みは両対応"),
    LayoutPath("legacy_terms_json", "analysis/terms.json", "file", "keep", "analyze-commit",
               "旧形式 JSON"),
    LayoutPath("legacy_plot_json", "analysis/plot-[ネタバレあり].json", "file", "keep",
               "analyze-commit", "旧形式 JSON"),
    LayoutPath("legacy_done", "output/_done/", "dir", "work", "commit", "旧配置。purge が消す"),
    LayoutPath("legacy_qa", "output/_qa/", "dir", "work", "qa", "旧配置。purge が消す"),
    LayoutPath("legacy_images", "images/", "dir", "work", "ingest",
               "旧配置。assemble が output/images/ へ移して消す"),
    LayoutPath("misplaced_house_style", "house_style.toml", "file", "keep", "agent",
               "誤配置。ツールは読まない。house_style.py init で _house/ へ移す"),
)

#: ツールが読まない作業領域（置いても壊れないが、成果物でもない）
WORKSPACE: tuple[LayoutPath, ...] = (
    LayoutPath("ws_notes", "_saga/_notes/", "dir", "keep", "agent",
               "シリーズ共通のメモ・ブリーフ"),
    LayoutPath("ws_tools", "_saga/_tools/", "dir", "keep", "agent",
               "使い捨ての補助スクリプト（スキルの scripts/ ではない）"),
    LayoutPath("ws_review", "_saga/_review/", "dir", "keep", "agent", "検収の作業ファイル"),
    LayoutPath("ws_series", "_saga/_series/", "dir", "keep", "agent",
               "シリーズ合本・目次などの編纂物"),
    LayoutPath("ws_backup", "_saga/_backup_*/", "dir", "keep", "agent",
               "退避ディレクトリ（_saga 直下の慣習名）"),
    LayoutPath("ws_house_notes", "_saga/_house/PARENT_NOTES.md", "file", "keep", "agent",
               "シリーズ共通の申し送り（慣習名。ツールは読まない）"),
    LayoutPath("ws_house_glossary", "_saga/_house/SERIES_GLOSSARY.md", "file", "keep",
               "agent", "シリーズ共通の用語メモ（慣習名。ツールは読まない）"),
    LayoutPath("ws_house_backup", "_saga/_house/_series_backup_*/", "dir", "keep", "agent",
               "house_style の退避（慣習名。_house 直下に増やさない）"),
)

ALL: tuple[LayoutPath, ...] = OUTER + LAYOUT + LEGACY + WORKSPACE

#: 一覧の末尾に出す注意（新しい場所を作る前に読む）
WORKSPACE_NOTE = (
    "作業領域はツールが読まない。新しいディレクトリを作る前に、既存の場所で足りないか確認する"
    "（判断メモ → _work/notes.md、補助スクリプト → _saga/_tools/、検収の作業 → _saga/_review/）。"
    "作品直下・analysis/・_work/ にツール管理外のファイルを増やさない。"
)

#: コード中の `work / "…"` の第1セグメントとして現れてよい名前
TOP_LEVEL = tuple(sorted({e.path.split("/")[0] for e in ALL}))


#: コード中の `work / "…"` を拾う（paths.py 自身は除く）
_LITERAL_RE = re.compile(r'(?:Path\(\s*work\s*\)|\bwork\b)\s*/\s*"([^"/]+)"')


def missing_literals() -> dict[str, list[str]]:
    """コードにあって paths.py に未登録の `work / "…"` を返す（空なら一致）。

    新しいトップレベルのパスを無言で足すと、md も一覧も追従しない。
    `--check` とテスト（scripts/tests/test_paths_layout.py）がこれを使う。
    """
    out: dict[str, list[str]] = {}
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        if f.name == "paths.py":
            continue
        for m in _LITERAL_RE.finditer(f.read_text(encoding="utf-8")):
            seg = m.group(1)
            if seg not in TOP_LEVEL:
                out.setdefault(seg, []).append(f.name)
    return out


def _p(e: LayoutPath) -> str:
    """表示用パス。`_saga/` 始まりは入力フォルダ側（<work>/ の外）なので付けない。"""
    return e.path if e.path.startswith("_saga/") else "<work>/" + e.path


def format_list(stream=None) -> None:
    """一覧（1行1件）。"""
    out = stream or sys.stdout
    for title, items in (("ツール管理（keep / work / transient）", LAYOUT),
                         ("旧配置（読めるが新規に作らない）", LEGACY),
                         ("作業領域（入力フォルダ側。<work>/ の外。ツールは読まない）", WORKSPACE)):
        print(f"## {title}", file=out)
        for e in items:
            print(f"{e.lifetime:9s} {_p(e):45s} {e.kind:4s} {e.writer:16s} {e.note}",
                  file=out)
        if items is WORKSPACE:
            print(f"  ※ {WORKSPACE_NOTE}", file=out)
        print(file=out)


def _tree_lines() -> list[str]:
    """<work>/ の木（md 用）。親ディレクトリは子から補って1回だけ出す。"""
    entries = [e for e in LAYOUT if not e.id.endswith("_stem")]
    emitted: dict[str, bool] = {}
    lines: list[str] = []

    def emit_dir(path: str) -> None:
        if path in emitted:
            return
        parts = path.split("/")
        for i in range(1, len(parts)):
            emit_dir("/".join(parts[:i]))
        emitted[path] = True
        note = next((e.note for e in entries
                     if e.path.rstrip("/") == path and e.path.endswith("/")), "")
        name = parts[-1] + "/"
        lines.append("  " * (len(parts) - 1) + name + (f"  # {note}" if note else ""))

    for e in entries:
        p = e.path.rstrip("/")
        if e.path.endswith("/"):
            emit_dir(p)
            continue
        parts = p.split("/")
        for i in range(1, len(parts)):
            emit_dir("/".join(parts[:i]))
        name = parts[-1]
        mark = {"work": "  # 作業中のみ", "transient": "  # 一時", "keep": ""}[e.lifetime]
        lines.append("  " * (len(parts) - 1) + name + mark)
    return lines


def format_md(stream=None) -> None:
    """md に貼るための木（手で写さず、この出力を使う）。"""
    out = stream or sys.stdout
    print("```", file=out)
    print("<work>/", file=out)
    for ln in _tree_lines():
        print(ln, file=out)
    print("```", file=out)
    print(file=out)
    print("`<work>/` の外側（入力フォルダ側）:", file=out)
    print("```", file=out)
    for e in OUTER:
        print(e.path, file=out)
    print("```", file=out)
    print(file=out)
    print("注: 無印は完成後も残る。`# 作業中のみ` は assemble の purge で消える。", file=out)
    print("旧配置・作業領域は `python3 scripts/paths.py` の一覧を参照。", file=out)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--md" in args:
        format_md()
        return 0
    if "--check" in args:
        miss = missing_literals()
        if not miss:
            print("OK: コード中の work / \"…\" はすべて paths.py に登録済み")
            return 0
        print("未登録のパスがあります（paths.py に追加してください）:", file=sys.stderr)
        for seg, files in sorted(miss.items()):
            print(f"  {seg}: {', '.join(sorted(set(files)))}", file=sys.stderr)
        return 1
    if args and args[0] not in ("-h", "--help", "--list", "list"):
        print(f"エラー: 不明な引数: {args[0]}（--md か --list）", file=sys.stderr)
        return 2
    print("レイアウトの正本: scripts/paths.py（md に構造を書かない）")
    print("用法: python3 scripts/paths.py [--md | --list | --check]")
    print()
    format_list()
    return 0


if __name__ == "__main__":
    sys.exit(main())
