#!/usr/bin/env python3
"""由 CI 產生 handoff artifact —— executor 不用寫任何東西（守衛 G4）。

判準因此從「交件沒有自陳 => FAIL」改成「CI 沒產出 handoff artifact => FAIL」。
這是腳本判得動的，而且 executor 沒有「忘記寫」的空間。

實錄：CS05 PR#11 實測 163 passed / 0 failed、clone rc=0，仍判 FAIL ——
理由是沒有 head sha、沒有 base sha、沒有 mutation 清單 => 突變測試無從重跑。
同一輪還救了另一件事：驗收令誤以為該 PR「production diff 為空」，
實測非空、新增 261 行，而沒有 base sha 就切不開「本票新增」與「上游堆疊帶進來的」。

🔴 **v0.3.4：baseline 不再寫死 pytest**（與 `replay_manifest.py`／`next_dispatch.py` 同一根因）。
Node 專案硬跑 `python -m pytest` 會拿到 `passed=0, failed=0`，
而本檔的回傳判準是 `return 1 if baseline["failed"]`
=> **一個測試都沒跑到會被判成綠、handoff 照樣產出**。那是安靜地成功。
=> 指令改 `--test-cmd`／`TEST_CMD`；解析共用 `replay_manifest.parse_counts()`；
   **解析不出來或 0 passed 一律非零退出**。

用法:
    python3 make_handoff.py --out ci/handoff.json [--ticket CS06] [--role-token <tok>] \
        [--test-cmd "npm test --silent"]
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_manifest import (DEFAULT_TEST_CMD, parse_counts,   # noqa: E402
                             warn_default_cmd)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def baseline(test_cmd):
    """跑一次測試當 baseline。**解析不出來不得當成零失敗。**"""
    for pyc in Path(".").rglob("__pycache__"):
        subprocess.run(["rm", "-rf", str(pyc)])
    p = subprocess.run(shlex.split(test_cmd), capture_output=True, text=True)
    parsed = parse_counts(p.stdout + "\n" + p.stderr)
    if parsed is None:
        return {"passed": 0, "failed": 0, "rc": p.returncode,
                "test_cmd": test_cmd, "parse_error": True}
    passed, failed, _ = parsed
    return {"passed": passed, "failed": failed, "rc": p.returncode,
            "test_cmd": test_cmd, "parse_error": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="ci/handoff.json")
    ap.add_argument("--ticket", default="")
    ap.add_argument("--role-token", default="")
    ap.add_argument("--test-cmd", default=os.environ.get("TEST_CMD", "") or DEFAULT_TEST_CMD)
    args = ap.parse_args()

    warn_default_cmd(args.test_cmd)
    head = sh("git", "rev-parse", "HEAD")
    base = sh("git", "merge-base", "origin/main", "HEAD")
    changed = sh("git", "diff", "--name-only", f"{base}...{head}").split()

    handoff = {
        "schema": "handoff/1",
        "generated_by": "ci",          # 不是 executor 自陳
        "ticket": args.ticket,
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "pr": os.environ.get("PR_NUMBER", ""),
        "head_sha": head,
        "base_sha": base,
        "changed_files": changed,
        "production_diff_empty": not [
            f for f in changed
            if not f.startswith(("tests/", "ci/", ".github/", "docs/"))
        ],
        "baseline": baseline(args.test_cmd),
        "role_token": args.role_token,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(handoff, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps(handoff, ensure_ascii=False, indent=2))
    # baseline 不綠、解析不出來、或 0 passed 都沒有繼續的意義
    b = handoff["baseline"]
    if b.get("parse_error"):
        print(f"🔴 測試輸出解析不出 passed/failed（指令 `{b['test_cmd']}`）"
              f" —— 不得把「看不懂」當成「零失敗」。", file=sys.stderr)
        return 1
    if b["passed"] == 0:
        print(f"🔴 baseline 是 0 passed（指令 `{b['test_cmd']}`）"
              f" —— 確認這個指令真的跑得到本 repo 的測試（Node 專案別用 pytest）。", file=sys.stderr)
        return 1
    return 1 if b["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
