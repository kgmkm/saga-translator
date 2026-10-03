"""zh→ja 方向のターゲット側（日本語）チェック。qa_translation.py から使う。

_lib.find_non_jis_han と同じ基準（CP932 外＋言語 pack の NON_JIS_HAN）。
日本語訳に残った簡体字・原文の誤字・未登録の固有名詞を検出する。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _lib import find_non_jis_han  # noqa: E402

__all__ = ["find_non_jis_han"]
