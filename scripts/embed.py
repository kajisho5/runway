#!/usr/bin/env python3
"""scripts/asc.py を apple-testflight.yml のヒアドキュメントに埋め込む。

呼び出し側リポから private な runway のファイルは読めないため、共通ワークフローには asc.py を丸ごと埋め込んでいる。
  python3 scripts/embed.py          埋め込みを更新
  python3 scripts/embed.py --check  埋め込みが scripts/asc.py と一致しているか確認（selfcheck 用）
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "asc.py"
WF = ROOT / ".github" / "workflows" / "apple-testflight.yml"
PATTERN = re.compile(r"(?P<indent>[ ]*)# >>> asc\.py[^\n]*\n.*?(?P=indent)# <<< asc\.py\n", re.S)


def render(indent):
    body = SRC.read_text(encoding="utf-8")
    if "\nPY\n" in body or body.startswith("PY\n"):
        sys.exit("asc.py にヒアドキュメント終端 'PY' 行があるので埋め込めない")
    lines = [f"{indent}# >>> asc.py (scripts/asc.py から scripts/embed.py で生成。直接編集しない)"]
    for line in body.splitlines():
        lines.append(f"{indent}{line}" if line else "")
    lines.append(f"{indent}# <<< asc.py")
    return "\n".join(lines) + "\n"


def main():
    text = WF.read_text(encoding="utf-8")
    m = PATTERN.search(text)
    if not m:
        sys.exit("apple-testflight.yml に埋め込みマーカーが無い")
    new = text[:m.start()] + render(m.group("indent")) + text[m.end():]
    if "--check" in sys.argv:
        if new != text:
            sys.exit("apple-testflight.yml の asc.py が古い。python3 scripts/embed.py を実行してコミットする")
        print("埋め込みは最新")
        return
    WF.write_text(new, encoding="utf-8")
    print("埋め込みを更新した")


if __name__ == "__main__":
    main()
