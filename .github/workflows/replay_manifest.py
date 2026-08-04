#!/usr/bin/env python3
"""站 5 自閉環的終判：重放 mutation manifest。

不讀報告、不看 session ID —— 把 manifest 每條 patch 實際套上去、跑一次測試、
比對宣稱與實測是否一致。

作弊要偽造一組【真的會紅】的突變，而那等價於真的做了工作。
=> 作弊成本 >= 誠實成本。

🔴 **v0.3.4：測試指令與輸出解析不再寫死 pytest。**
成因（與 `next_dispatch.py` 同一個根因）：`Prematch_LoL` 是 **Node 專案**。
舊版硬跑 `python -m pytest`，在 Node repo 上拿到的是 `passed=0, failed=0`，
而 baseline 的判準只有 `failed != 0` ⇒ **「一個測試都沒跑到」被判成 baseline 綠。**
🔴 **這正是本檔在防的形狀：安靜地成功。**
⇒ 指令改 `--test-cmd`（或 `TEST_CMD` 環境變數），輸出解析改成多 runner 適配，
**解析不出數字就 fail-closed**，而且 **baseline `passed == 0` 一律 FAIL**。

用法:
    python3 replay_manifest.py --manifest ci/manifests/<sha>.json \
        --repo-root . --ticket-scope cs_live/ [--survival-threshold 0.15] \
        [--test-cmd "npm test --silent"]

Exit: 0 = PASS, 1 = FAIL, 2 = 用法/環境錯誤
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

SCHEMA = "mutation-manifest/1"
BLOCKING_CATEGORY = "silent-failure"
DEFAULT_TEST_CMD = f"{sys.executable} -m pytest -q --tb=no -rf"


def run(cmd, cwd=None, check=False):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        fail(f"命令失敗: {' '.join(cmd)}\n{p.stderr[:2000]}")
    return p


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def parse_counts(out):
    """從測試輸出解析 (passed, failed, red_test_ids)。回 None 表示【解析不出來】。

    🔴 **`None` 與 `(0, 0, [])` 必須分開** —— 舊版把兩者混成同一件事，
    於是「這個 runner 我看不懂」被當成「零失敗」。**那是安靜地成功。**

    ⚠️ **具名代價**：只認得下列 runner 的摘要格式。其他 runner ⇒ 回 None ⇒ 呼叫端 fail-closed。
    要支援新 runner，在這裡加一段，**不要在呼叫端放寬**。
    """
    passed = failed = None
    red = []

    # pytest： "1 failed, 155 passed in 0.42s" / "FAILED tests/test_x.py::test_y"
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("FAILED "):
            red.append(s.split()[1])
        m = re.search(r"(\d+)\s+passed", s)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+)\s+failed", s)
        if m:
            failed = int(m.group(1))

    # jest / vitest： "Tests:  1 failed, 22 passed, 23 total"
    if passed is None:
        m = re.search(r"Tests?:\s*(?:(\d+)\s+failed,\s*)?(\d+)\s+passed", out)
        if m:
            failed = int(m.group(1) or 0)
            passed = int(m.group(2))
    if not red:
        red += [x.strip() for x in re.findall(r"^\s*[✕×✗]\s+(.+)$", out, re.M)]

    # node --test / TAP： "# pass 12" / "# fail 0"
    if passed is None:
        mp = re.search(r"^#\s*pass\s+(\d+)", out, re.M)
        mf = re.search(r"^#\s*fail\s+(\d+)", out, re.M)
        if mp:
            passed, failed = int(mp.group(1)), int(mf.group(1)) if mf else 0

    # go test -v： "--- PASS: TestX" / "--- FAIL: TestY"
    if passed is None:
        gp = re.findall(r"^--- PASS:\s+(\S+)", out, re.M)
        gf = re.findall(r"^--- FAIL:\s+(\S+)", out, re.M)
        if gp or gf:
            passed, failed = len(gp), len(gf)
            red += gf

    if passed is None:
        return None
    return passed, (failed or 0), red


def warn_default_cmd(test_cmd):
    """用到寫死的預設值時要**吵**。靜默的預設值就是「寫死 pytest」換一個位置藏起來。"""
    if test_cmd == DEFAULT_TEST_CMD and not os.environ.get("TEST_CMD"):
        print("⚠️  沒有指定 --test-cmd／TEST_CMD，退回預設 `%s`。"
              "\n    🚫 這個預設【只對 Python 專案成立】。Node／Go 專案請明寫指令，"
              "否則你量到的是「沒有測試」不是「測試通過」。" % DEFAULT_TEST_CMD,
              file=sys.stderr)


def test_counts(root, test_cmd):
    """跑一次測試，回 (passed, failed, red_test_ids)。

    每次都先清 __pycache__ —— 長度不變的突變會與原檔同 (mtime, size)，
    Python 會重用舊的 .pyc，量到的會是上一個突變的結果。
    （Node 沒有這個問題，但清掉不花錢。）
    """
    for pyc in Path(root).rglob("__pycache__"):
        run(["rm", "-rf", str(pyc)])
    p = subprocess.run(shlex.split(test_cmd), cwd=root,
                       capture_output=True, text=True)
    parsed = parse_counts(p.stdout + "\n" + p.stderr)
    if parsed is None:
        fail(f"測試輸出解析不出 passed/failed —— 指令 `{test_cmd}`。\n"
             f"🔴 **不得把「看不懂」當成「零失敗」。**\n"
             f"要嘛換一個會印摘要的指令，要嘛在 `parse_counts()` 補這個 runner 的格式。\n"
             f"--- stdout 尾段 ---\n{p.stdout[-1200:]}")
    return parsed


def clean_tree(root):
    """還原到乾淨狀態，並確認 git diff 為空。"""
    run(["git", "checkout", "--", "."], cwd=root)
    d = run(["git", "diff", "--stat"], cwd=root)
    if d.stdout.strip():
        fail(f"還原後 git diff 非空 —— 突變污染了工作樹:\n{d.stdout[:1000]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--ticket-scope", default="",
                    help="逗號分隔的路徑前綴；票宣告的 production diff 範圍")
    ap.add_argument("--survival-threshold", type=float, default=0.15)
    ap.add_argument("--expected-role-token", default="")
    # 🔴 不寫死 pytest：CI 從 tickets.yml 的 test_cmd 帶進來（與 next_dispatch 同一個宣告）
    ap.add_argument("--test-cmd", default=os.environ.get("TEST_CMD", "") or DEFAULT_TEST_CMD,
                    help="跑測試的指令；預設吃環境變數 TEST_CMD，再退回 pytest")
    args = ap.parse_args()

    warn_default_cmd(args.test_cmd)
    root = Path(args.repo_root).resolve()
    mpath = Path(args.manifest)
    if not mpath.exists():
        fail(f"manifest 不存在: {mpath} —— 沒有自陳就沒有可驗收的東西（G4）")

    try:
        m = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"manifest 不是合法 JSON: {e}")

    if m.get("schema") != SCHEMA:
        fail(f"schema 不符: 期待 {SCHEMA}，實得 {m.get('schema')!r}")

    # --- role_token: 防「兩人剛好挑同一角色」的安靜失敗 ---
    if args.expected_role_token:
        got = (m.get("producer") or {}).get("role_token", "")
        if got != args.expected_role_token:
            fail("role_token 不符或缺漏 —— 無法確認雙審真的是兩個角色各審一次")

    # --- base_sha 必須等於 CI 自己算的 merge-base ---
    declared_base = m.get("base_sha", "")
    head = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    mb = run(["git", "merge-base", "origin/main", "HEAD"], cwd=root).stdout.strip()
    if not declared_base:
        fail("manifest 沒有 base_sha —— 切不開『本票新增』與『上游堆疊 PR 帶進來的』")
    if mb and not declared_base.startswith(mb[:8]) and not mb.startswith(declared_base[:8]):
        fail(f"base_sha 與實測 merge-base 不符: 宣稱 {declared_base[:8]}，實測 {mb[:8]}")

    # --- diff 是否在票宣告的範圍內 ---
    if args.ticket_scope:
        scopes = [s.strip() for s in args.ticket_scope.split(",") if s.strip()]
        changed = run(["git", "diff", "--name-only", f"{mb}...{head}"],
                      cwd=root).stdout.split()
        # 測試檔與 CI 產物一律允許
        allowed_extra = ("tests/", "ci/", ".github/")
        outside = [f for f in changed
                   if not any(f.startswith(s) for s in scopes)
                   and not f.startswith(allowed_extra)]
        if outside:
            fail(f"diff 超出票宣告的範圍: {outside[:10]}")

    # --- baseline 必須真的綠 ---
    clean_tree(root)
    b_pass, b_fail, _ = test_counts(root, args.test_cmd)
    if b_fail != 0:
        fail(f"baseline 不綠: {b_pass} passed / {b_fail} failed（指令 `{args.test_cmd}`）")
    # 🔴 v0.3.4：0 passed 也要擋。舊版只看 failed != 0 ⇒ 一個測試都沒跑到會被判成綠，
    #   而那正是「跑錯 runner」的形狀（Node repo 硬跑 pytest ⇒ 0/0）。
    if b_pass == 0:
        fail(f"baseline 是 0 passed —— 沒有測試地基就沒有可重放的東西。"
             f"確認 `{args.test_cmd}` 真的跑得到這個 repo 的測試（Node 專案別用 pytest）")
    declared = m.get("baseline", {})
    if declared.get("passed") not in (None, b_pass):
        fail(f"baseline passed 宣稱 {declared.get('passed')}，實測 {b_pass}")

    muts = m.get("mutations", [])
    if not muts:
        fail("manifest 沒有任何 mutation —— N 本身就是 finding，但 0 不能算通過")

    counted = surviving = 0
    mismatches, blocking, uncounted = [], [], []

    for mu in muts:
        mid = mu.get("id", "?")
        patch = mu.get("patch")
        if not patch:
            fail(f"{mid}: 缺 patch 欄位 —— 沒有 patch 就沒有機械重放")

        clean_tree(root)
        p = subprocess.run(["git", "apply", "-"], cwd=root, input=patch,
                           capture_output=True, text=True)
        if p.returncode != 0:
            clean_tree(root)
            mismatches.append(f"{mid}: patch 套不上（{p.stderr.strip()[:200]}）")
            continue

        a_pass, a_fail, red = test_counts(root, args.test_cmd)
        clean_tree(root)

        expected_red = set(mu.get("expected_red") or [])
        # 存活 = 沒紅，或紅的不是我要驗的那件事
        survived = (a_fail == 0) or (expected_red and not (expected_red & set(red)))

        claimed = mu.get("actual") or {}
        if claimed.get("failed") is not None and claimed["failed"] != a_fail:
            mismatches.append(
                f"{mid}: 宣稱 failed={claimed['failed']}，實測 {a_fail}")
        if mu.get("survived") is not None and bool(mu["survived"]) != survived:
            mismatches.append(
                f"{mid}: 宣稱 survived={mu['survived']}，實測 {survived}")

        if survived:
            if not mu.get("repro"):
                # 寫不出 repro 的存活突變 = 疑似 equivalent mutant，不計分子也不計分母
                uncounted.append(mid)
                continue
            surviving += 1
            if mu.get("category") == BLOCKING_CATEGORY:
                blocking.append(f"{mid} {mu.get('file')}:{mu.get('line')} — {mu.get('repro')}")
        counted += 1

    clean_tree(root)

    rate = (surviving / counted) if counted else 0.0
    print(json.dumps({
        "ticket": m.get("ticket"),
        "counted": counted,
        "surviving": surviving,
        "survival_rate": round(rate, 4),
        "uncounted_no_repro": uncounted,
        "blocking_silent_failures": blocking,
        "mismatches": mismatches,
    }, ensure_ascii=False, indent=2))

    if mismatches:
        fail("宣稱與實測不符 —— PASS 的意思是「自陳與實測一致」，"
             f"沒有一致就沒有 PASS:\n  " + "\n  ".join(mismatches))
    if blocking:
        fail("存在會安靜地錯的存活突變（G1 擋門類）:\n  " + "\n  ".join(blocking))
    if rate > args.survival_threshold:
        fail(f"存活率 {rate:.2%} 超過門檻 {args.survival_threshold:.2%}")

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
