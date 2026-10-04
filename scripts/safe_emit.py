"""翻訳ブロックの確定を共有ロック付きで直列実行するラッパー（safe_emit）。

確定は saga.py の共通確定処理（align / QA / 残渣の検査込み、#17）に一本化した。
複数の翻訳エージェントが同一作品を並行で進めても chapters_meta の
書き込み競合が起きないようにする。ロックは saga.py と同じ共有ロック
（<work>/_work/.lock、_lib に一元化）を使う。

使い方:
  python3 safe_emit.py <work_dir> --chapter chXXX --block bN [--model M]

終了コード:
  0 = 検査・確定・記録の完了
  1 = 検査で停止（指摘を直して再実行）
  2 = ロック取得失敗（リトライ）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import acquire_lock, release_lock, utf8  # noqa: E402


def main() -> int:
    utf8()
    ap = argparse.ArgumentParser(description="safe emit: 共通確定処理（検査込み）in one lock")
    ap.add_argument("work", type=Path, help="作品ディレクトリ（_saga/ 内部）")
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--block", required=True)
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--prompt-version", default="v3")
    ap.add_argument("--tokens-in", type=int, default=12000)
    ap.add_argument("--tokens-out", type=int, default=5000)
    args = ap.parse_args()

    args.work.mkdir(parents=True, exist_ok=True)

    ok, reason = acquire_lock(args.work, "safe_emit")
    if not ok:
        print(f"[safe_emit] ロック取得に失敗: {reason}", file=sys.stderr)
        return 2
    try:
        from saga import _finalize_block
        rc = _finalize_block(args.work, args.chapter, args.block,
                             args.model, args.prompt_version,
                             tokens_in=args.tokens_in, tokens_out=args.tokens_out)
        if rc != 0:
            print("[safe_emit] 検査で停止したため確定しませんでした（出力は変更していません）",
                  file=sys.stderr)
            return 1
        return 0
    except Exception as e:
        print(f"[safe_emit] エラー: {e}", file=sys.stderr)
        return 1
    finally:
        release_lock(args.work)


if __name__ == "__main__":
    raise SystemExit(main())