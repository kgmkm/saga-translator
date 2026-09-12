"""章メタデータへの版記録(prompt_version / model / 日時 / トークン)。

status の自動更新は qa_translation --emit が行う。こちらは status を触らず、
translation フィールドに版情報のみ記録する（record_meta 忘れても status が
ズレないこと、record_meta の実行自体をユーザーが忘れないことを分離する）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import load_chapters_meta, save_chapters_meta, utf8  # noqa: E402


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="版記録")
    ap.add_argument("work")
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--block", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt-version", required=True)
    ap.add_argument("--tokens-in", type=int, default=None)
    ap.add_argument("--tokens-out", type=int, default=None)
    args = ap.parse_args()

    work = Path(args.work)
    meta = load_chapters_meta(work)
    rec = {
        "model": args.model,
        "prompt_version": args.prompt_version,
        "translated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tokens_in": args.tokens_in or 0,
        "tokens_out": args.tokens_out or 0,
    }
    for c in meta["chapters"]:
        if c["id"] == args.chapter:
            for b in c["blocks"]:
                if b["id"] == args.block:
                    b["translation"] = rec
            save_chapters_meta(work, meta)
            print(f"[meta] {args.chapter}/{args.block}: {rec}")
            return
    raise SystemExit(f"章が見つかりません: {args.chapter}")


if __name__ == "__main__":
    main()
