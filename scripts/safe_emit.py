"""段落QA(--emit)と版記録(record_meta)をロック付きで直列実行するラッパー。

複数の翻訳エージェントが同一作品を並行で進めても chapters_meta の
書き込み競合が起きないようにする。ロックは saga.py と同じ共有ロック
（<work>/_work/.lock、_lib に一元化）を使う。

使い方:
  python3 safe_emit.py <work_dir> --chapter chXXX --block bN [--model M]

終了コード:
  0 = QA PASS & emit & record 完了
  1 = QA FAIL（指摘を直して再実行）
  2 = ロック取得失敗（リトライ）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import acquire_lock, release_lock  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="safe emit: QA(--emit) + record_meta in one lock")
    ap.add_argument("work", type=Path, help="作品ディレクトリ（_saga/ 内部）")
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--block", required=True)
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--prompt-version", default="v3")
    ap.add_argument("--tokens-in", type=int, default=12000)
    ap.add_argument("--tokens-out", type=int, default=5000)
    args = ap.parse_args()

    scripts_dir = Path(__file__).parent
    args.work.mkdir(parents=True, exist_ok=True)

    ok, reason = acquire_lock(args.work, "safe_emit")
    if not ok:
        print(f"[safe_emit] ロック取得に失敗: {reason}", file=sys.stderr)
        return 2
    try:
        qa = subprocess.run(
            [sys.executable, str(scripts_dir / "qa_translation.py"), str(args.work),
             "--chapter", args.chapter, "--block", args.block, "--emit"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        sys.stdout.write(qa.stdout or "")
        sys.stderr.write(qa.stderr or "")
        if qa.returncode != 0:
            print("[safe_emit] QA FAIL → emit せず終了", file=sys.stderr)
            return 1
        rec = subprocess.run(
            [sys.executable, str(scripts_dir / "record_meta.py"), str(args.work),
             "--chapter", args.chapter, "--block", args.block,
             "--model", args.model, "--prompt-version", args.prompt_version,
             "--tokens-in", str(args.tokens_in), "--tokens-out", str(args.tokens_out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        sys.stdout.write(rec.stdout or "")
        sys.stderr.write(rec.stderr or "")
        return 0 if rec.returncode == 0 else 1
    finally:
        release_lock(args.work)


if __name__ == "__main__":
    raise SystemExit(main())