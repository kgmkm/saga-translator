"""saga-translator 共有ライブラリ。標準ライブラリのみ。"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "saga-translator"
CONFIG_PATH = CONFIG_DIR / "config.toml"
SKILL_VERSION = "0.8.0"

DEFAULT_CONFIG = """# saga-translator 設定(認証情報は持たない)
[translation]
style = "plain"          # plain=常体 / polite=敬体
ruby = "first"           # first=人名・固有名詞の初出のみ / always / none
proper_noun = "phonetic" # phonetic=音訳 / semantic=意訳 / keep=原語併記

[context]
prev_tail_paras = 3      # 前章/前ブロックの訳文末尾を何段落入れるか
next_head_paras = 2      # 次章/次ブロックの冒頭を何段落入れるか

[split]
block_target_chars = 4000  # ブロック分割の目標文字数(段落境界で切る)

[qa]
strict_paragraph = true    # 段落数不一致を不合格にする
"""


def _load_lang_packs() -> dict:
    """scripts/lang/<lang>.py を登録する。循環importを避けるため遅延しない(pack側は _lib をimportしない)。"""
    packs: dict = {}
    try:
        from lang import zh as pack_zh
        packs["zh"] = pack_zh
    except ImportError:
        pass
    try:
        from lang import ja as pack_ja
        packs["ja"] = pack_ja
    except ImportError:
        pass
    return packs


LANG_PACKS = _load_lang_packs()


# 対応する翻訳方向 (src, dst)。manifest.dst が無ければ src から推定する。
DST_BY_SRC = {"zh": "ja", "ja": "zh"}


def direction(work) -> tuple[str, str]:
    """(原語, 訳文言語) を返す。manifest が無ければ zh→ja（後方互換）。"""
    try:
        man = load_structured(Path(work) / "manifest")
    except (FileNotFoundError, OSError):
        man = {}
    src = man.get("lang") or "zh"
    dst = man.get("dst") or DST_BY_SRC.get(src, "ja")
    return src, dst


def target_suffix(dst: str) -> str:
    """訳文ファイルの接尾辞（_ja / _zh）。未知の言語は _<dst>。"""
    return {"ja": "_ja", "zh": "_zh"}.get(dst, f"_{dst}")


def target_quotes(dst: str) -> tuple[str, str]:
    """訳文の台詞引用符。(開き, 閉じ)。"""
    return {"zh": ("“", "”")}.get(dst, ("「", "」"))


def target_title(titles: dict, dst: str) -> str:
    """titles.toml から訳文タイトル。dst 欄を優先し、もう一方で補う。"""
    other = "ja" if dst == "zh" else "zh"
    return ((titles.get(f"title_{dst}") or titles.get(f"title_{other}")) or "").strip()


def chapter_title(entry: dict, dst: str, fallback: str = "") -> str:
    """章見出しの訳文。dst 欄を優先し、もう一方・原文で補う。"""
    other = "ja" if dst == "zh" else "zh"
    return entry.get(dst) or entry.get(other) or fallback


# 会話判定の段落頭（方向別）。中国語原文は “…/[…] 括り、日本語原文は「…」。
_DIALOGUE_HEADS = {
    ("zh", "ja"): (("“", "[", "［"), ("「", "『", "（", "(")),
    ("ja", "zh"): (("「", "『", "（", "("), ("“", "（", "(")),
}


def dialogue_heads(src: str, dst: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(原文側の会話頭, 訳文側の会話頭)。未知の方向は zh→ja 扱い。"""
    return _DIALOGUE_HEADS.get((src, dst), _DIALOGUE_HEADS[("zh", "ja")])


def align_ratio_floor(src: str, dst: str) -> float:
    """check_align の文字数比下限。中国語は日本語より短くなる。"""
    return 0.30 if (src, dst) == ("ja", "zh") else 0.45


def utf8() -> None:
    """WindowsコンソールでもCJKを出力できるようにする。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _toml_str(v: str) -> str:
    v = (v or "").replace("\\", "\\\\").replace('"', '\\"')
    v = v.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{v}"'


def _toml_value(v) -> str:
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "''"
    if isinstance(v, dict):
        # インラインテーブル(配列ofテーブル内のネスト用)
        return "{ " + ", ".join(f"{k2} = {_toml_value(v2)}" for k2, v2 in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ValueError(f"TOML非対応の型: {type(v)}")


def dump_toml(data: dict) -> str:
    """最小限のTOMLシリアライザ(テーブル+配列ofテーブル)。Python標準ライブラリのみで書き出す。"""
    out: list[str] = []
    for key, val in data.items():
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            for item in val:
                out.append(f"[[{key}]]")
                out.extend(f"{k2} = {_toml_value(v2)}" for k2, v2 in item.items())
                out.append("")
        elif isinstance(val, list) and not val:
            out.append(f"{key} = []")
            out.append("")
        elif isinstance(val, dict):
            out.append(f"[{key}]")
            out.extend(f"{k2} = {_toml_value(v2)}" for k2, v2 in val.items())
            out.append("")
        else:
            out.append(f"{key} = {_toml_value(val)}")
            out.append("")
    return "\n".join(out)


def load_toml(path: Path) -> dict:
    import tomllib
    p = Path(path)
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        from textwrap import dedent
        msg = str(e)
        line = ""
        try:
            for i, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if not line:
                    line = raw
        except OSError:
            pass
        raise TomlSyntaxError(
            f"{p}\nTOMLの構文が壊れています: {msg}\n"
            f"AIエージェントはこのファイルを読み、TOMLとして妥当な形に修正してから再実行してください。\n"
            f"(先頭行: {line[:80]})"
        ) from e


class TomlSyntaxError(RuntimeError):
    """ユーザー編集で壊れたTOML。AIが拾ってLLMで直す。"""


def _first_lines(path: Path, n: int = 1) -> str:
    try:
        return " / ".join(l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()[:1] not in ("#", "") and l.strip())[:n * 80]
    except (OSError, IndexError):
        return ""


def load_toml(path: Path) -> dict:
    import tomllib
    p = Path(path)
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise TomlSyntaxError(
            f"{p}\n"
            f"TOMLの構文が壊れています: {e}\n"
            f"AIエージェントはこのファイルを読み、TOMLとして妥当な形に修正して保存し、コマンドを再実行すること。\n"
            f"(内容の先頭: {_first_lines(p, 2)[:160]})"
        ) from e


def validate_expected(path: Path, data: dict, expect: dict) -> None:
    """最低限の構造チェック。expect: {key: 'dict'|'list'|'list_of_dicts'|'str'}。壊れていたら TomlSyntaxError。"""
    for key, kind in expect.items():
        v = data.get(key, None)
        if kind == "optional":
            continue
        if v is None:
            raise TomlSyntaxError(
                f"{path}\n必須キー `{key}` がありません。AIがファイルを読み、欄を補って保存し、再実行すること"
            )
        if kind == "dict" and not isinstance(v, dict):
            raise TomlSyntaxError(f"{path}\n`{key}` はテーブル([{key}])であるべきです。今は {type(v).__name__}")
        if kind == "list" and not isinstance(v, list):
            raise TomlSyntaxError(f"{path}\n`{key}` は配列([[{key}]])であるべきです。今は {type(v).__name__}")
        if kind == "list_of_dicts":
            if not isinstance(v, list):
                raise TomlSyntaxError(f"{path}\n`{key}` は配列ofテーブル([[{key}]])であるべきです。今は {type(v).__name__}")
            for i, item in enumerate(v):
                if not isinstance(item, dict):
                    raise TomlSyntaxError(f"{path}\n`{key}[{i}]` はテーブルであるべきです。今は {type(item).__name__}")


def save_toml(path: Path, obj) -> None:
    atomic_write_text(path, dump_toml(obj))


def _pair(path: Path) -> tuple[Path, Path]:
    """(toml, json) のパス。`ch001.paras` のように中間拡張子があっても .toml を足す。"""
    p = Path(path)
    suf = p.suffix.lower()
    if suf == ".toml":
        return p, p.with_suffix(".json")
    if suf == ".json":
        return p.with_suffix(".toml"), p
    return Path(str(p) + ".toml"), Path(str(p) + ".json")


def load_structured(path: Path) -> dict:
    """同名の .toml を優先し、無ければ .json（旧成果物）。"""
    toml_p, json_p = _pair(path)
    if toml_p.exists():
        return load_toml(toml_p)
    if json_p.exists():
        return load_json(json_p)
    raise FileNotFoundError(f"{toml_p} / {json_p}")


def save_structured(path: Path, obj) -> None:
    """TOMLで書き、旧JSONがあれば消す。"""
    toml_p, json_p = _pair(path)
    save_toml(toml_p, obj)
    if json_p.exists():
        json_p.unlink()


def load_chapters_meta(work: Path) -> dict:
    p = Path(work) / "chapters_meta"
    data = load_structured(p)
    validate_expected(p, data, {"chapters": "list_of_dicts"})
    for i, c in enumerate(data["chapters"]):
        if not isinstance(c.get("id"), str) or not isinstance(c.get("blocks"), list):
            raise TomlSyntaxError(
                f"{p}\n`chapters[{i}]` に `id`(文字列) と `blocks`(配列) が必要です。AIが構造を修復して再実行すること"
            )
    return data


def save_chapters_meta(work: Path, obj) -> None:
    save_structured(Path(work) / "chapters_meta", obj)


def block_paras(paras_data: dict, block: dict) -> list[dict]:
    """paras.toml の内容から block 範囲（`[a, b]` 段落番号）の段落を取り出す。

    段落番号は 1 始まり。範囲外は無視する（欠番はそのまま返す）。
    """
    rng = block.get("paras") or []
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return []
    try:
        lo, hi = int(rng[0]), int(rng[1])
    except (TypeError, ValueError):
        return []
    return [
        p for p in (paras_data.get("paras") or [])
        if isinstance(p, dict) and isinstance(p.get("n"), int) and lo <= p["n"] <= hi
    ]


def block_src_hash(paras_data: dict, block: dict) -> str:
    """ブロック原文（paragraphs）の sha256 先頭12桁。原文が変われば値も変わる。

    改行は LF に正規化し、段落番号と本文を `n\\ttext` の形で連結してからハッシュ化する。
    """
    import hashlib
    parts = [f"{p['n']}\t{p.get('text', '')}" for p in block_paras(paras_data, block)]
    blob = "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def block_meta(meta: dict, block_key: str) -> dict | None:
    """chapters_meta の該当 block 辞書を返す（無ければ None）。"""
    for ch in meta.get("chapters", []):
        cid = ch.get("id")
        for b in ch.get("blocks", []):
            if f"{cid}/{b.get('id')}" == block_key:
                return b
    return None


def load_paras(work: Path, cid: str) -> dict:
    p = Path(work) / "chapters" / f"{cid}.paras"
    data = load_structured(p)
    paras = data.get("paras")
    if not isinstance(paras, list) or not all(
        isinstance(x, dict) and isinstance(x.get("n"), int) and isinstance(x.get("text"), str) for x in paras
    ):
        raise TomlSyntaxError(
            f"{p}\n`paras` は配列ofテーブル({{n=1, text=\"…\"}})であるべきです。AIが構造を修復して再実行すること"
        )
    return data


def save_paras(work: Path, cid: str, obj) -> None:
    save_structured(Path(work) / "chapters" / f"{cid}.paras", obj)


def _load_analysis_file(work: Path, names: list[str], empty: dict) -> dict:
    adir = Path(work) / "analysis"
    for name in names:
        p = adir / name
        if not p.exists():
            continue
        data = load_json(p) if p.suffix.lower() == ".json" else load_toml(p)
        if not data:
            # コメント行のみの空ファイルは「未分析」と同じ扱い。entries 等の必須キーを
            # 要求すると、最初の追記(analyze-commit)が循環して失敗する。
            return empty
        if name.startswith("characters"):
            validate_expected(p, data, {"characters": "list_of_dicts"})
        elif name.startswith("terms"):
            validate_expected(p, data, {"terms": "list_of_dicts"})
        elif name.startswith("plot"):
            validate_expected(p, data, {"entries": "list_of_dicts"})
        elif name == "titles.toml":
            expect = {"chapters": "list_of_dicts"} if "chapters" in data else {}
            for _tk in ("title_ja", "title_zh"):
                if _tk in data:
                    expect[_tk] = "str"
            if expect:
                validate_expected(p, data, expect)
        return data
    return empty


def load_characters(work: Path) -> dict:
    return _load_analysis_file(work, [
        "characters-[ネタバレあり].toml",
        "characters.toml",
        "characters.json",
    ], {"characters": []})


def load_terms(work: Path) -> dict:
    return _load_analysis_file(work, ["terms.toml", "terms.json"], {"terms": []})


def load_plot(work: Path) -> dict:
    return _load_analysis_file(work, [
        "plot-[ネタバレあり].toml",
        "plot-[ネタバレあり].json",
    ], {"entries": []})


def load_titles(work: Path) -> dict:
    return _load_analysis_file(work, ["titles.toml"], {})


def characters_toml_path(work: Path) -> Path:
    adir = Path(work) / "analysis"
    for name in ("characters-[ネタバレあり].toml", "characters.toml"):
        p = adir / name
        if p.exists():
            return p
    return adir / "characters-[ネタバレあり].toml"


def _under_work(work: Path, path: Path) -> Path:
    work_r = Path(work).resolve()
    p = Path(path).resolve()
    p.relative_to(work_r)
    return p


def all_blocks_emitted(work: Path, meta: dict) -> bool:
    chapters = meta.get("chapters") or []
    if not chapters:
        return False
    work = Path(work)
    suf = target_suffix(direction(work)[1])
    for c in chapters:
        for b in c.get("blocks") or []:
            if not (work / "output" / c["id"] / f"{b['id']}{suf}.md").exists():
                return False
    return True


def purge_source(work: Path, keep_parallel: bool = False) -> list[str]:
    """全ブロック確定後に原文と中間物を消す。入力元ファイルは触らない。
    残す: 訳文(章/全文/目次)、analysis、manifest、output/images、chapters_meta、logs。
    keep_parallel=True のときは output/ 直下の対訳(全文_対訳.md・章対訳)も残す。"""
    work = Path(work).resolve()
    if not (work / "manifest.toml").exists() and not (work / "manifest.json").exists() and not (work / "chapters_meta.toml").exists() and not (work / "chapters_meta.json").exists():
        raise ValueError(f"作品ディレクトリに見えない: {work}")
    removed: list[str] = []

    def rm(rel: str) -> None:
        p = _under_work(work, work / rel)
        if not p.exists():
            return
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        removed.append(rel.replace("\\", "/"))

    for rel in ("originals", "translation_input", "chapters", "output/_work", "output/_done", "output/_qa"):
        # commit が確定ドラフトを _work/done/ にアーカイブする（レビュー P3-2。
        # emit 後の検証・時間分析・復元に使う）。done は消さずに退避して戻す。
        if rel == "output/_work":
            done_p = work / "output" / "_work" / "done"
            if done_p.is_dir():
                keep = work / "output" / "_work_done_keep"
                if keep.exists():
                    shutil.rmtree(keep)
                keep.mkdir(parents=True)
                shutil.move(str(done_p), str(keep / "done"))
                rm(rel)
                (work / "output" / "_work").mkdir(parents=True, exist_ok=True)
                shutil.move(str(keep / "done"), str(done_p))
                shutil.rmtree(keep, ignore_errors=True)
                continue
        rm(rel)
    out = work / "output"
    if out.exists():
        if not keep_parallel:
            for p in list(out.rglob("*")):
                if not p.is_file():
                    continue
                name = p.name
                if "parallel" in name.lower() or name == "全文_対訳.md":
                    _under_work(work, p).unlink()
                    removed.append(str(p.relative_to(work)).replace("\\", "/"))
        for p in list(out.iterdir()):
            if p.is_dir() and p.name.startswith("ch"):
                _under_work(work, p)
                shutil.rmtree(p)
                removed.append(str(p.relative_to(work)).replace("\\", "/"))
        # 旧配置の work/images(存在すれば)を output/images へ退避してから消す
        old_imgs = work / "images"
        if old_imgs.is_dir():
            new_imgs = work / "output" / "images"
            new_imgs.mkdir(parents=True, exist_ok=True)
            for f in old_imgs.iterdir():
                if f.is_file():
                    dest = new_imgs / f.name
                    if not dest.exists():
                        shutil.move(str(f), str(dest))
                        removed.append(f"images(moved) -> {dest.name}")
            shutil.rmtree(old_imgs)
    return removed


def atomic_write_text(path: Path, text: str) -> None:
    """一時ファイル経由で書き込む(完了ファイルだけが残る)。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {"translation": {}, "context": {}, "split": {}, "qa": {}}


def _fallback_encodings() -> list[str]:
    out: list[str] = []
    for enc in ("utf-8-sig", "utf-8"):
        out.append(enc)
    for pack in LANG_PACKS.values():
        for enc in getattr(pack, "EXTRA_ENCODINGS", ()):
            if enc not in out:
                out.append(enc)
    if "shift_jis" not in out:
        out.append("shift_jis")
    return out


def read_text_auto(path: Path) -> tuple[str, str]:
    """文字コード自動判定。戻り値: (テキスト, エンコーディング名)"""
    raw = Path(path).read_bytes()
    last = _fallback_encodings()[-1]
    for enc in _fallback_encodings():
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            if enc == last:
                break
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def guess_lang(text: str) -> dict:
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    hangul = sum(1 for c in text if "\uac00" <= c <= "\ud7af")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    if kana >= 3 and kana * 3 > han:
        lang = "ja"
    elif hangul > latin and hangul > han:
        lang = "ko"
    elif han > latin * 2:
        lang = "zh"
    else:
        lang = "en"
    result = {
        "lang": lang,
        "han_script": None,
        "stats": {"han": han, "kana": kana, "hangul": hangul, "latin": latin},
    }
    pack = LANG_PACKS.get(lang)
    if pack and hasattr(pack, "refine"):
        pack.refine(text, result)
    return result


def find_non_jis_han(text: str) -> list[tuple[str, int]]:
    """日本語に無い漢字(CP932/JIS X 0208 に無いCJK)を検出する。

    中国語→日本語の翻訳で、登場人物表に無い原語(誤字・未登録の固有名詞・
    簡体字の混入)が訳文に残るのを機械的に拾う。登場人物表の原語チェック
    (qa_translation)は登録済みの名前しか見ないため、この走査が網を掛ける。
    戻り値: [(文字, 出現回数)]。JIS収録だが日本人に読めない字(筱 等)は
    言語packの NON_JIS_HAN で補う。
    """
    counts: dict[str, int] = {}
    extra = ""
    for pack in LANG_PACKS.values():
        extra += getattr(pack, "NON_JIS_HAN", "") or ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\uf900" <= ch <= "\ufaff":
            try:
                ch.encode("cp932")
            except UnicodeEncodeError:
                counts[ch] = counts.get(ch, 0) + 1
        if extra and ch in extra:
            counts[ch] = counts.get(ch, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def split_paras(text: str) -> list[str]:
    """空行区切りで段落に分割。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = []
    for p in re.split(r"\n[ \t]*\n+", text):
        p = p.strip()
        if p:
            paras.append(p)
    return paras


SEP_LINE = re.compile(r"^\s*[-—ー*＝=]{3,}.*$")
BARE_SEP = re.compile(r"^\s*[-*＊＝=]{5,}\s*$")


def parse_novel_header(text: str) -> tuple[dict | None, str]:
    """ダウンロード形式のヘッダ(タイトル/作者/URL/日付/#タグ/区切り線)を検出する。
    特定サイトに依存しない共通形式。戻り値: (メタデータ or None, 本文)"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sep_idx = None
    for i, line in enumerate(lines):
        if SEP_LINE.match(line) and any(k in line for k in ("以下", "正文", "开始", "開始", "开始")):
            sep_idx = i
            break
        if BARE_SEP.match(line) and i >= 3:
            above = "\n".join(lines[:i])
            if "#R-18" in above or "http" in above or re.search(r"^\d{4}-\d{2}-\d{2}", above, re.M):
                sep_idx = i
                break
    if sep_idx is None:
        return None, text
    header = lines[:sep_idx]
    body = "\n".join(lines[sep_idx + 1:])
    meta: dict = {}
    hi = 0
    while hi < len(header) and not header[hi].strip():
        hi += 1
    if hi < len(header):
        meta["title"] = header[hi].strip()
        hi += 1
    for line in header[hi:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("http"):
            meta["source_url"] = line
        elif re.match(r"^\d{4}-\d{2}-\d{2}", line):
            meta["date"] = line
        elif line.startswith("#"):
            meta.setdefault("tags", []).append(line)
        elif "author" not in meta and len(line) <= 40:
            meta["author"] = line
    return meta, body


def parse_filename(path: Path) -> dict:
    """`19360197-タイトル【1】.txt` 形式からID・題・部数を推定。"""
    stem = Path(path).stem
    d: dict = {}
    m = re.match(r"^(\d{3,})[-_](.+)$", stem)
    if m:
        d["novel_id"] = m.group(1)
        rest = m.group(2)
    else:
        rest = stem
    pm = re.search(r"【(\d+)】", rest)
    if pm:
        d["part"] = pm.group(1)
        rest = rest.replace(pm.group(0), " ")
    d["title"] = rest.strip(" -_")
    return d


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """`---` で囲まれた YAML 風 frontmatter をメタとして読み、本文を返す。

    `title: …` / `author: …` / `tags: [a, b]` 等の単純な `key: value` のみ扱う
    （YAML パーサは持たないため、スカラーとインライン配列だけ）。先頭が `---`
    で無い、または閉じ `---` が無い場合は (None, text) を返す。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    meta: dict = {}
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip("\"'") for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        else:
            meta[key] = val.strip("\"'")
    body = "\n".join(lines[end + 1:])
    return meta, body


def read_skill_version(skill_root) -> str | None:
    """スキルルートの SKILL.md frontmatter から version を返す。無ければ None。"""
    try:
        text = (Path(skill_root) / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
    meta, _ = parse_frontmatter(text)
    if not meta:
        return None
    v = meta.get("version")
    return str(v).strip() if v else None


def skill_commit(skill_root) -> str:
    """スキルリポジトリの git HEAD 短縮 SHA。git 不可・リポジトリ外なら "unknown"。"""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", str(Path(skill_root)), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if r.returncode != 0:
        return "unknown"
    sha = r.stdout.strip()
    return sha or "unknown"


# 共通の章見出し。言語固有は scripts/lang/<lang>.py の CHAPTER_ALTS
COMMON_CHAPTER_ALTS = r"[Cc]hapter\s*\d+|[Cc]HAPTER\s*\d+"
NUM_LINE = re.compile(r"^\s*[0-9０-９]{1,4}\s*$")
# 場面転換記号(ブロック分割の優先境界)
SCENE_BREAK = re.compile(r"^\s*(?:[-*＊※ー=─]{3,}|…{2,}|·{3,}|・{3,}|[○●◇◆▼▲☆★※✦]|\[chapter:[^\]]*\]|\[newpage\])\s*$")


def compile_chapter_re(lang: str | None = None) -> re.Pattern:
    alts = [COMMON_CHAPTER_ALTS]
    if lang is None:
        for pack in LANG_PACKS.values():
            extra = getattr(pack, "CHAPTER_ALTS", "") or ""
            if extra:
                alts.append(extra)
    else:
        pack = LANG_PACKS.get(lang)
        extra = getattr(pack, "CHAPTER_ALTS", "") or "" if pack else ""
        if extra:
            alts.append(extra)
    return re.compile(r"^\s*(?:" + "|".join(alts) + r")\s*.*$")


CHAPTER_RE = compile_chapter_re(None)


def find_chapters(paras: list[str], lang: str | None = None) -> list[tuple[str | None, int, int]]:
    """段落列から章の区間を検出。戻り値: [(タイトル, 開始idx, 終了idx)]
    lang を渡すとその言語packの見出しだけ足す。未指定なら全packを足す(後方互換)。"""
    heading = compile_chapter_re(lang)
    heads: list[tuple[str, int]] = []
    for i, p in enumerate(paras):
        first_line = p.split("\n", 1)[0]
        if heading.match(first_line) or NUM_LINE.match(first_line):
            heads.append((first_line.strip(), i))
    if not heads:
        return [(None, 0, len(paras))]
    chapters = []
    for j, (title, start) in enumerate(heads):
        end = heads[j + 1][1] if j + 1 < len(heads) else len(paras)
        chapters.append((title, start, end))
    if heads[0][1] > 0:
        chapters.insert(0, (None, 0, heads[0][1]))
    return chapters


def make_blocks(paras: list[str], target_chars: int) -> list[tuple[int, int]]:
    """段落idx区間 [start, end) を目標文字数ごとにブロック分割。
    段落境界で切り、近くに場面転換記号があればそこを優先する。"""
    blocks: list[tuple[int, int]] = []
    start = 0
    n = len(paras)
    while start < n:
        chars = 0
        end = start
        last_break = None
        while end < n and chars < target_chars:
            chars += len(paras[end])
            if SCENE_BREAK.match(paras[end]):
                last_break = end
            end += 1
        if end >= n:
            blocks.append((start, n))
            break
        cut = end
        if last_break is not None and last_break + 1 > start + 2 and last_break + 1 < end:
            cut = last_break + 1
        blocks.append((start, cut))
        start = cut
    return blocks


def record_timeline(work, stage: str, chapter=None, block=None,
                    started=None, ended=None, exit_code: int = 0,
                    retries: int = 0, commit_id=None, reason=None,
                    mode=None) -> None:
    """logs/timeline.jsonl に1行追記。失敗は握りつぶす。

    reason: commit 等の失敗理由の分類（成功時は "success"）。None なら欄を出さない。
    mode: prepare のモード（translation / analysis 等）。None なら欄を出さない。
    """
    try:
        log_dir = Path(work) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if started is None and ended is None:
            ended = datetime.now(timezone.utc)
            started = ended
        elif ended is None:
            ended = datetime.now(timezone.utc)
        elif started is None:
            started = ended
        dur = int((ended - started).total_seconds() * 1000)
        entry = {
            "ts": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_ms": dur,
            "stage": stage,
        }
        if chapter is not None:
            entry["chapter"] = chapter
        if block is not None:
            entry["block"] = block
        entry["exit_code"] = exit_code
        if retries:
            entry["retries"] = retries
        if commit_id is not None:
            entry["commit_id"] = commit_id
        if reason is not None:
            entry["reason"] = reason
        if mode is not None:
            entry["mode"] = mode
        with open(log_dir / "timeline.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


@contextmanager
def timed_stage(work, stage, chapter=None, block=None, reason=None, mode=None):
    """main() を囲むコンテキストマネージャ。開始・終了を記録し、exit_code を決定する。

    reason / mode は簡易記録用（成功時は reason="success" を明示したい場合のみ渡す）。
    commit のように失敗理由を動的に分類する工程は record_timeline 直呼びを使う。
    """
    started = datetime.now(timezone.utc)
    code = 0
    try:
        yield
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        raise
    except BaseException:
        code = 1
        raise
    finally:
        record_timeline(work, stage, chapter=chapter, block=block,
                        started=started, ended=datetime.now(timezone.utc),
                        exit_code=code, reason=reason, mode=mode)


# ---------------------------------------------------------------------------
# 共有排他ロック（workspace 単位で1つ: <work>/_work/.lock）
#
# ロックファイルには所有者情報（PID・ホスト名・取得時刻・工程名）を JSON で書く。
# 排他失敗は False（呼び出し側が exit 3 にする）。
# stale（LOCK_STALE_SECONDS 超）でも所有者の生存確認が取れて初めて上書きする:
#   - 同一ホスト: PID の生存を確認（Windows=ctypes の OpenProcess、POSIX=os.kill(pid,0)）
#   - 所有者が生存 → 上書きしない
#   - 生死判定不能（別ホスト・権限不足等）→ 上書きしない（推測で上書きしない）
# ---------------------------------------------------------------------------

LOCK_STALE_SECONDS = 600.0

try:  # 実行環境のホスト名（別ホスト判定に使う。失敗しても致命ではない）
    _HOSTNAME = socket.gethostname()
except OSError:  # pragma: no cover - 環境依存
    _HOSTNAME = ""


def lock_path(work) -> Path:
    """共有ロックファイルのパス（<work>/_work/.lock）。"""
    return Path(work) / "_work" / ".lock"


def _read_lock_owner(path: Path) -> dict | None:
    """ロックファイルの所有者情報を読む。

    新形式: JSON（pid/host/acquired_at/stage）。
    旧形式: PID を1行書いただけのテキスト（後方互換。host 不明として扱う）。
    読めない場合は None。
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if isinstance(data, dict):
            return data
        return None
    # 旧形式: 先頭行の整数を PID とみなす
    first = raw.splitlines()[0].strip()
    if first.isdigit():
        return {"pid": int(first), "host": "", "acquired_at": None, "stage": ""}
    return None


def _pid_alive(pid: int) -> bool | None:
    """PID のプロセスが生きているか。True=生存 / False=死亡 / None=判定不能。

    標準ライブラリのみを使う。別ユーザー等で権限不足のときは None を返し、
    呼び出し側が「上書きしない」判断に使う。
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # プロセスは存在するがシグナル送出の権限がない → 生存
        return True
    except OSError:
        return None
    return True


def _pid_alive_windows(pid: int) -> bool | None:
    """Windows: ctypes の OpenProcess / GetExitCodeProcess で生存確認。"""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - 通常は到達しない
        return None

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    OpenProcess = kernel32.OpenProcess
    OpenProcess.restype = wintypes.HANDLE
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    GetExitCodeProcess = kernel32.GetExitCodeProcess
    GetExitCodeProcess.restype = wintypes.BOOL
    GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    CloseHandle = kernel32.CloseHandle
    CloseHandle.restype = wintypes.BOOL
    CloseHandle.argtypes = [wintypes.HANDLE]

    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 取得不可: プロセス不在か、権限不足か区別できない。
        # ERROR_INVALID_PARAMETER(87) は PID 不在を意味する。
        if ctypes.get_last_error() == 87:
            return False
        return None
    code = wintypes.DWORD()
    try:
        if not GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == STILL_ACTIVE
    finally:
        CloseHandle(handle)


def _owner_is_confirmed_dead(owner: dict | None) -> bool:
    """所有者が死んでいることを確認できた場合のみ True を返す。

    - 別ホスト（host が記録されていて自ホストと違う）→ False（判定不能扱い）
    - host 未記録（旧形式）→ 自ホストのプロセスとみなして生存確認する
    - 生存 → False / 死亡 → True / 判定不能 → False
    """
    if not owner:
        return False
    host = owner.get("host")
    if host and host != _HOSTNAME:
        return False
    pid = owner.get("pid")
    alive = _pid_alive(pid) if isinstance(pid, int) else None
    return alive is False


def _write_lock_owner(path: Path, stage: str) -> None:
    """所有者情報を書く（原子的に置換）。"""
    payload = json.dumps({
        "pid": os.getpid(),
        "host": _HOSTNAME,
        "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": stage,
    }, ensure_ascii=False)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        # 置換に失敗しても直接書く
        try:
            path.write_text(payload + "\n", encoding="utf-8")
        except OSError:
            pass
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def acquire_lock(work, stage: str = "") -> tuple[bool, str | None]:
    """共有ロックを取得する。

    戻り値: (取得できたか, 失敗理由)。成功時は (True, None)。
    排他失敗（所有者生存・判定不能を含む）時は (False, 理由文字列) を返す。
    呼び出し側は理由を stderr に出し exit 3 とする。
    """
    path = lock_path(work)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1) 新規作成（排他）
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        _write_lock_owner(path, stage)
        return True, None
    except FileExistsError:
        pass
    except OSError as e:
        return False, f"ロックファイルを開けません: {e}"

    # 2) 既存ロックの stale 判定
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        # stat できない（直後に消えた等）→ 再取得を試みる
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            _write_lock_owner(path, stage)
            return True, None
        except OSError:
            return False, "ロックの状態を確認できません"

    if age <= LOCK_STALE_SECONDS:
        owner = _read_lock_owner(path)
        pid = (owner or {}).get("pid")
        return False, f"他の書き手がロック中です（PID {pid}）"

    # stale: 所有者の死亡を確認できた場合のみ上書き
    owner = _read_lock_owner(path)
    if not _owner_is_confirmed_dead(owner):
        pid = (owner or {}).get("pid")
        host = (owner or {}).get("host") or ""
        if pid and host and host != _HOSTNAME:
            return False, f"stale ですが所有者 PID {pid} は別ホスト {host} のため生存確認できません"
        if pid:
            return False, f"stale ですが所有者 PID {pid} は生存中"
        return False, "stale ですが所有者の生死を判定できません"

    # 死亡確認済み → 上書きして取得
    try:
        _write_lock_owner(path, stage)
    except OSError as e:
        return False, f"stale ロックの上書きに失敗: {e}"
    return True, None


def release_lock(work) -> None:
    """自分が所有するロックのみ解放する。他人のロックは消さない。"""
    path = lock_path(work)
    try:
        if not path.exists():
            return
        owner = _read_lock_owner(path)
        if not owner or owner.get("pid") != os.getpid():
            return
        path.unlink()
    except OSError:
        pass
