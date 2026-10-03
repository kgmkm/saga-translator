#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""連作版ビルダー — saga-translator の確定出力を、連作として読みやすい1フォルダにまとめる。

使い方:
  python3 build_series_edition.py <root> --check
  python3 build_series_edition.py <root> --dest <dst> [--title T] [--note TEXT] [--repair]

<root>: シリーズ親ディレクトリ(_saga/ を含む) または _saga/ 自体。
出力: <dst>/ 目次.md 合本.md NN_<訳題>.md images/

標準ライブラリのみ。ネットワーク不使用。source は --repair 指定時のみ書き換える。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCAFFOLD_RE = re.compile(r"^(?:<!--\s*ch\d+\s*-->|#\s*ch\d+)$")
HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
WORK_MARKERS = ("manifest.toml", "manifest.json", "chapters_meta.toml", "chapters_meta.json")
ILLEGAL_CHARS = '<>:"/\\|?*'
FULLWIDTH_MAP = {"(": "（", ")": "）", "#": "＃", "%": "％"}


class BuildError(RuntimeError):
    pass


# ---------------------------------------------------------------- utilities

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def write_eol(p: Path, text: str, eol: str) -> None:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if eol != "\n":
        text = text.replace("\n", eol)
    p.write_bytes(text.encode("utf-8"))


def toml_string(path: Path, key: str, section: str | None = None):
    """TOML から文字列を取り出す。tomllib があれば使い、無ければ正規表現。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
        data = tomllib.loads(text)
        if section:
            data = data.get(section) or {}
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    m = re.search(r'^\s*%s\s*=\s*"((?:[^"\\]|\\.)*)"' % re.escape(key), text, re.M)
    return m.group(1).strip() if m else None


def toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ref_path(raw: str) -> str:
    return raw.strip().split(' "')[0]


# ------------------------------------------------------------- non-JIS scan

def _load_non_jis():
    """saga-translator の _lib.find_non_jis_han を探す。無ければ None（CP932 版で代替）。"""
    candidates = []
    env = os.environ.get("SAGA_TRANSLATOR_SCRIPTS")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve().parent
    for up in (here.parent, here.parent.parent, here.parent.parent.parent):
        candidates.append(up / "saga-translator" / "scripts")
    for c in candidates:
        try:
            if (c / "_lib.py").exists():
                if str(c) not in sys.path:
                    sys.path.insert(0, str(c))
                from _lib import find_non_jis_han  # type: ignore
                return find_non_jis_han
        except Exception:
            continue
    return None


_NON_JIS_FN = _load_non_jis()


def scan_non_jis(text: str):
    if _NON_JIS_FN is not None:
        try:
            return list(_NON_JIS_FN(text))
        except Exception:
            pass
    counts: dict[str, int] = {}
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\uf900" <= ch <= "\ufaff":
            try:
                ch.encode("cp932")
            except UnicodeEncodeError:
                counts[ch] = counts.get(ch, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


# --------------------------------------------------------------- discovery

def looks_like_work(d: Path) -> bool:
    return any((d / m).exists() for m in WORK_MARKERS)


def find_saga(root: Path) -> Path:
    root = root.resolve()
    if not root.is_dir():
        raise BuildError(f"ディレクトリではない: {root}")

    def has_works(p: Path) -> bool:
        try:
            return any(d.is_dir() and not d.name.startswith("_") and looks_like_work(d) for d in p.iterdir())
        except OSError:
            return False

    sub = root / "_saga"
    if has_works(sub):
        return sub
    if has_works(root):
        return root
    raise BuildError(f"作品ディレクトリ（_saga）が見つからない: {root}")


def list_works(saga: Path):
    built, unfinished = {}, []
    for d in sorted(saga.iterdir(), key=lambda p: p.name):
        if not d.is_dir() or d.name.startswith("_") or not looks_like_work(d):
            continue
        if (d / "output" / "全文_訳文.md").exists():
            built[d.name] = d
        else:
            unfinished.append(d.name)
    return built, unfinished


def parse_progress(saga: Path):
    """進捗.md の表から 収録順・訳題・全行 を取る。無ければ None。"""
    p = saga / "進捗.md"
    if not p.exists():
        return None
    order, titles, rows = [], {}, []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        num, wid, src, ja = int(cells[0]), cells[1], cells[2], cells[3]
        rows.append((num, wid, src, ja))
        if wid.isdigit():
            order.append(wid)
            if ja and ja not in ("—", "-", ""):
                titles[wid] = ja
    if not order:
        return None
    return {"order": order, "titles": titles, "rows": rows}


def resolve_order(built, explicit, prog):
    if explicit:
        missing = [w for w in explicit if w not in built]
        if missing:
            raise BuildError(f"--works の作品がビルド対象に見つからない: {missing}")
        return explicit, "--works"
    if prog:
        order = [w for w in prog["order"] if w in built]
        extra = [w for w in built if w not in order]
        if order:
            return order + sorted(extra), "進捗.md の表"
    return sorted(built, key=lambda w: (int(w) if w.isdigit() else 1 << 62, w)), "ID昇順（既定）"


def resolve_title(work: Path, wid: str, prog):
    """訳題の解決: titles.toml → 全文の見出し → 進捗.md 訳題 → 原題（要確認）。"""
    t = toml_string(work / "analysis" / "titles.toml", "title_ja") or toml_string(work / "analysis" / "titles.toml", "title_zh")
    if t:
        return t, "titles.toml"
    full = work / "output" / "全文_訳文.md"
    if full.exists():
        for ln in read_text(full).split("\n")[:16]:
            m = HEADING_RE.match(ln.strip())
            if m and not re.fullmatch(r"ch\d+", m.group(1)):
                return m.group(1).strip(), "全文の見出し"
    if prog and wid in prog["titles"]:
        return prog["titles"][wid], "進捗.md 訳題"
    src = (toml_string(work / "chapters_meta.toml", "title", section="work")
           or toml_string(work / "manifest.toml", "title"))
    return (src or f"作品{wid}"), "原題（要確認）"


# ---------------------------------------------------------------- normalize

def normalize_body(text: str) -> str:
    """足場除去 → 行末空白除去 → `---` の前後に空行確保 → 空行圧縮 → 前後空白除去。見出しは触らない。

    `---` の直前が非空行だと markdown は setext 見出しとして描画してしまうため、
    水平線として確実に機能するよう空行を補う（描画の修正のみで本文は変えない）。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for ln in text.split("\n"):
        if SCAFFOLD_RE.match(ln.strip()):
            continue
        lines.append(ln.rstrip())
    padded = []
    for ln in lines:
        if ln.strip() == "---":
            if padded and padded[-1].strip():
                padded.append("")
            padded.append(ln)
            padded.append("")
        else:
            padded.append(ln)
    coll = []
    for ln in padded:
        if not ln.strip() and coll and not coll[-1].strip():
            continue
        coll.append(ln)
    while coll and not coll[0].strip():
        coll.pop(0)
    while coll and not coll[-1].strip():
        coll.pop()
    return "\n".join(coll) + "\n"


def split_heading(body: str):
    if not body:
        return None
    first = body.split("\n", 1)[0].strip()
    m = HEADING_RE.match(first)
    if m and not re.fullmatch(r"ch\d+", m.group(1)):
        return m.group(1).strip()
    return None


def sanitize_filename(name: str) -> str:
    out = []
    for ch in name:
        if ch in ILLEGAL_CHARS or ord(ch) < 32:
            out.append("_")
        elif ch in FULLWIDTH_MAP:
            out.append(FULLWIDTH_MAP[ch])
        else:
            out.append(ch)
    s = "".join(out).strip().rstrip(". ")
    return s or "part"


def link_target(filename: str) -> str:
    return filename.replace(" ", "%20")


def strip_seam_lines(s: str) -> str:
    lines = s.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and lines[0].strip() == "---":
        lines.pop(0)
    while lines and lines[-1].strip() == "---":
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def build_part_body(src_text: str, title: str):
    """全文を整形して単話本文を作る。(body, heading_or_None, prepended) を返す。"""
    body = normalize_body(src_text)
    heading = split_heading(body)
    if heading is not None:
        return body, heading, False
    if not body.strip():
        return "# " + title + "\n", None, True
    return "# " + title + "\n\n" + body, None, True


def setext_hazards(text: str):
    """`---` の直前が非空行だと setext 見出しとして描画される。その行番号を返す。"""
    lines = text.replace("\r\n", "\n").split("\n")
    bad = []
    for i, l in enumerate(lines):
        if l.strip() == "---" and i > 0 and lines[i - 1].strip():
            bad.append(i + 1)
    return bad


def guard_destructive(dst: Path) -> None:
    rp = dst.resolve()
    if len(rp.parts) < 3 or rp == Path.home().resolve() or rp.parts[0].lower() == rp.anchor.lower().rstrip('/\\'):
        raise BuildError(f"危険な出力先のため --force を拒否: {rp}")


# ------------------------------------------------------------------- repair

def repair_sources(infos, prog) -> None:
    """titles.toml 欠落と全文・目次の見出し欠落を補修する（明示時のみ）。"""
    for rec in infos:
        work, wid, title = rec["work"], rec["wid"], rec["title"]
        tp = work / "analysis" / "titles.toml"
        if not tp.exists():
            if rec["title_src"] in ("全文の見出し", "進捗.md 訳題"):
                src = (toml_string(work / "chapters_meta.toml", "title", section="work")
                       or toml_string(work / "manifest.toml", "title") or "")
                try:
                    from _lib import direction as _direction
                    _dst = _direction(work)[1]
                except Exception:
                    _dst = "ja"
                body = (
                    "# 作品タイトル。\n\n"
                    f'title_src = "{toml_escape(src)}"\n'
                    f'title_{_dst} = "{toml_escape(title)}"\n'
                    f'note = "連作版ビルド時に補完（{rec["title_src"]}から解決）"\n'
                )
                tp.parent.mkdir(parents=True, exist_ok=True)
                write_eol(tp, body, "\n")
                print(f"[repair] titles.toml を作成: {wid} 「{title}」")
            else:
                print(f"[repair] {wid}: 訳題が原題しか解決できないため titles.toml は作らない")
                continue
        for name in ("全文_訳文.md", "目次.md"):
            p = work / "output" / name
            if not p.exists():
                continue
            text = read_text(p)
            eol = detect_eol(text)
            first = next((l.strip() for l in text.replace("\r\n", "\n").split("\n") if l.strip()), "")
            if first != "# " + title:
                write_eol(p, "# " + title + "\n\n" + text, eol)
                print(f"[repair] 見出しを追記: {wid}/{name}")


# ------------------------------------------------------------------- build

def copy_images(work: Path, dst_images: Path, wid: str, collisions: list) -> int:
    src = work / "output" / "images"
    if not src.is_dir():
        return 0
    n = 0
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        tgt = dst_images / f.name
        if tgt.exists():
            collisions.append((f.name, wid))
            continue
        shutil.copy2(f, tgt)
        n += 1
    return n


def cmd_build(args, saga: Path, built, unfinished, infos, order_src, prog) -> int:
    dst = Path(args.dest).expanduser().resolve()
    if dst.exists() and any(dst.iterdir()):
        if not args.force:
            raise BuildError(f"出力先が既存で空ではない: {dst}（--force で消して再ビルド）")
        guard_destructive(dst)
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    images_dir = dst / "images"
    images_dir.mkdir(exist_ok=True)
    eol = "\r\n" if args.eol == "crlf" else "\n"
    series_title = args.title or dst.name

    collisions: list = []
    for rec in infos:
        rec["n_images"] = copy_images(rec["work"], images_dir, rec["wid"], collisions)
    if collisions:
        raise BuildError("画像名が衝突（統合先で同名）: "
                         + ", ".join(f"{n}（{w}）" for n, w in collisions)
                         + " → 片方をリネームしてから再実行")

    parts, part_bodies = [], []
    width = max(2, len(str(len(infos))))
    for i, rec in enumerate(infos, 1):
        src_text = read_text(rec["work"] / "output" / "全文_訳文.md")
        body, heading, prepended = build_part_body(src_text, rec["title"])
        rec["prepended"] = prepended
        if heading is not None and heading != rec["title"]:
            print(f"[warn] {rec['wid']}: 全文の見出し「{heading}」と訳題「{rec['title']}」が不一致（見出しを採用）")
            rec["title"] = heading
        rec["file"] = f"{i:0{width}d}_{sanitize_filename(rec['title'])}.md"
        write_eol(dst / rec["file"], body, eol)
        parts.append(rec)
        part_bodies.append(body)

    # 合本
    note_hon = args.note_hon if args.note_hon is not None else args.note
    head = "# " + series_title
    if note_hon:
        head += "\n\n" + note_hon
    gomoku = "\n\n---\n\n".join([head] + [strip_seam_lines(b) for b in part_bodies]) + "\n"
    if "\n---\n---\n" in gomoku:
        raise BuildError("合本の継ぎ目が二重になった（内部エラー）")
    write_eol(dst / "合本.md", gomoku, eol)

    # 目次
    toc = ["# " + series_title]
    if args.note:
        toc += ["", args.note]
    if getattr(args, "url", None):
        toc += ["", args.url]
    toc += ["", "# 目次", ""]
    for rec in parts:
        toc.append(f"- [{rec['title']}]({link_target(rec['file'])})")
    toc += ["", f"{len(parts)}作品を1ファイルで読む： [合本.md](合本.md)", ""]
    if prog and not args.no_untranslated:
        built_ids = set(built)
        untr = [r for r in prog["rows"] if r[1] not in built_ids]
        if untr:
            toc += ["# 未翻訳（原題）", ""]
            for num, wid, src, ja in untr:
                label = src if src not in ("—", "-", "") else (wid if not wid.isdigit() else "（不明）")
                toc.append(f"- #{num} {label}")
            toc.append("")
    if args.footer:
        toc += ["---", "", args.footer]
    write_eol(dst / "目次.md", "\n".join(toc) + "\n", eol)

    # 検証
    problems, warnings = ([], [])
    if not args.no_verify:
        problems, warnings = verify_build(dst, parts, built, eol)

    # 非JIS 走査（情報）
    findings = []
    for rec in parts:
        nj = scan_non_jis(read_text(dst / rec["file"]))
        if nj:
            findings.append((rec["file"], nj))
    for name in ("合本.md", "目次.md"):
        nj = scan_non_jis(read_text(dst / name))
        if nj:
            findings.append((name, nj))

    # 報告
    print("== 連作版ビルド ==")
    print(f"saga: {saga}")
    print(f"dest: {dst}")
    print(f"順序: {len(infos)}作品（{order_src}）")
    for i, rec in enumerate(parts, 1):
        mark = "見出し補完" if rec.get("prepended") else "見出し既存"
        print(f"  {i:02d} {rec['wid']}  {rec['file']}  訳題={rec['title_src']}  {mark}  画像{rec['n_images']}")
    if unfinished:
        print(f"skip（全文_訳文.md が無い）: {', '.join(unfinished)}")
    print(f"合本: {len(IMG_RE.findall(read_text(dst / '合本.md')))} 画像参照 ／ images: {len(list(images_dir.iterdir()))} 枚")
    if findings:
        print("非JIS漢字（シリーズ名・未訳原題などの意図的メタデータ以外は要修正）:")
        for name, nj in findings:
            print(f"  {name}: {''.join(ch for ch, _ in nj)}")
    if warnings:
        for w in warnings:
            print(f"[warn] {w}")
    if problems:
        print("VERIFY FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("verify: OK")
    return 0


def verify_build(dst: Path, parts, built, eol):
    problems, warnings = [], []
    refs_total = 0
    used_names = set()
    for rec in parts:
        p = dst / rec["file"]
        raw = p.read_bytes()
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        if eol == "\r\n" and (raw.count(b"\n") - raw.count(b"\r\n")) > 0:
            problems.append(f"{rec['file']}: 孤立改行（CRLF でない行）がある")
        if not text.startswith("# "):
            problems.append(f"{rec['file']}: 見出しで始まらない")
        if any(SCAFFOLD_RE.match(l.strip()) for l in text.split("\n")):
            problems.append(f"{rec['file']}: 内部足場が残っている")
        if "\n\n\n" in text:
            problems.append(f"{rec['file']}: 空行が3連続")
        src_text = read_text(built[rec["wid"]] / "output" / "全文_訳文.md")
        body, _, _ = build_part_body(src_text, rec["title"])
        if text != body:
            problems.append(f"{rec['file']}: source と内容が不一致")
        for raw_ref in IMG_RE.findall(text):
            ref = ref_path(raw_ref)
            refs_total += 1
            used_names.add(ref)
            if not (dst / ref).exists():
                problems.append(f"{rec['file']}: 画像参照が解決しない {ref}")
    g = read_text(dst / "合本.md").replace("\r\n", "\n")
    if "\n---\n---\n" in g:
        problems.append("合本.md: --- の二重継ぎ目")
    hz = setext_hazards(g)
    if hz:
        problems.append(f"合本.md: setext 見出し化する --- が {len(hz)} 箇所（行 {hz[:5]}）")
    grefs = [ref_path(r) for r in IMG_RE.findall(g)]
    if len(grefs) != refs_total:
        problems.append(f"合本.md: 画像参照数 {len(grefs)} != 単話合計 {refs_total}")
    for ref in grefs:
        if not (dst / ref).exists():
            problems.append(f"合本.md: 画像参照が解決しない {ref}")
    for rec in parts:
        if f"# {rec['title']}" not in g:
            problems.append(f"合本.md: 見出しが無い「{rec['title']}」")
    toc = read_text(dst / "目次.md")
    for l in re.findall(r"\]\(([^)]+)\)", toc):
        if not (dst / l.replace("%20", " ")).exists():
            problems.append(f"目次.md: リンク切れ {l}")
    imgs_dir = dst / "images"
    if imgs_dir.is_dir():
        names = {f.name for f in imgs_dir.iterdir() if f.is_file()}
        orphans = sorted(names - {r.split("/", 1)[-1] for r in used_names})
        if orphans:
            warnings.append(f"images/: 未参照 {len(orphans)}枚 {orphans[:5]}")
    return problems, warnings


# -------------------------------------------------------------------- check

def cmd_check(saga: Path, built, unfinished, infos, order_src, prog) -> int:
    print("== 連作版 点検 ==")
    print(f"saga: {saga}")
    print(f"順序: {len(infos)}作品（{order_src}）")
    problems = []
    for i, rec in enumerate(infos, 1):
        work, wid = rec["work"], rec["wid"]
        text = read_text(work / "output" / "全文_訳文.md")
        refs = [ref_path(r) for r in IMG_RE.findall(text)]
        missing = [r for r in refs if not (work / "output" / r).exists()]
        nj = scan_non_jis(text)
        heading = split_heading(normalize_body(text))
        flags = []
        if rec["title_src"] == "原題（要確認）":
            flags.append("訳題未確定（titles.toml 無し）")
        if missing:
            flags.append(f"画像参照{len(missing)}件が欠落")
        if nj:
            flags.append(f"非JIS{len(nj)}字")
        status = " / ".join(flags) if flags else "OK"
        print(f"  {i:02d} {wid}  「{rec['title']}」  訳題={rec['title_src']}  "
              f"見出し={'あり' if heading else 'なし'}  画像{len(refs) - len(missing)}/{len(refs)}  → {status}")
        if rec["title_src"] == "原題（要確認）" or missing:
            problems.append(wid)
    if unfinished:
        print(f"skip（全文_訳文.md が無い）: {', '.join(unfinished)}")
    if prog:
        untr = [r for r in prog["rows"] if r[1] not in set(built)]
        print(f"未翻訳（進捗表）: {len(untr)}件")
    if problems:
        print(f"要対応: {', '.join(problems)}")
        return 1
    print("点検 OK")
    return 0


# --------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="saga-translator 連作版ビルダー")
    ap.add_argument("root", help="シリーズ親ディレクトリ(_saga/ を含む) または _saga/ 自体")
    ap.add_argument("--dest", help="出力先（ビルド時必須）")
    ap.add_argument("--title", help="シリーズ訳題（既定: dest のフォルダ名）")
    ap.add_argument("--note", default="", help="目次・合本に入れる注記（原作・URL など）")
    ap.add_argument("--url", default="", help="目次に入れるシリーズURL（注記の次の行）")
    ap.add_argument("--note-hon", dest="note_hon", help="合本だけ別の注記にする")
    ap.add_argument("--footer", default="", help="目次の末尾（翻訳情報など）")
    ap.add_argument("--works", help="収録順を作品IDのカンマ区切りで明示")
    ap.add_argument("--check", action="store_true", help="点検のみ（書込みなし）")
    ap.add_argument("--repair", action="store_true", help="source の titles.toml・見出しを補修")
    ap.add_argument("--force", action="store_true", help="既存 dest を消して再ビルド")
    ap.add_argument("--eol", choices=("crlf", "lf"), default="crlf", help="改行コード（既定 crlf）")
    ap.add_argument("--no-untranslated", action="store_true", help="未翻訳一覧を出さない")
    ap.add_argument("--no-verify", action="store_true", help="検証をスキップ（非推奨）")
    args = ap.parse_args(argv)

    try:
        saga = find_saga(Path(args.root))
        built, unfinished = list_works(saga)
        if not built:
            raise BuildError("ビルド対象（output/全文_訳文.md がある作品）が見つからない")
        prog = parse_progress(saga)
        explicit = [w.strip() for w in args.works.split(",") if w.strip()] if args.works else None
        order, order_src = resolve_order(built, explicit, prog)
        infos = []
        for wid in order:
            work = built[wid]
            title, tsrc = resolve_title(work, wid, prog)
            infos.append({"wid": wid, "work": work, "title": title, "title_src": tsrc})
        if args.check:
            return cmd_check(saga, built, unfinished, infos, order_src, prog)
        if not args.dest:
            raise BuildError("--dest が必要（点検だけなら --check）")
        if args.repair:
            repair_sources(infos, prog)
        return cmd_build(args, saga, built, unfinished, infos, order_src, prog)
    except BuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
