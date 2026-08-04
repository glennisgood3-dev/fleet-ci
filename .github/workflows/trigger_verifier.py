#!/usr/bin/env python3
"""由 CI 觸發站 5 的 mutation-verifier —— 觸發權在 CI，不在 executor。

為什麼不是 executor 自己委派：
  executor 自己派 verifier = 被審者選審查者。他可以挑弱 prompt、只給部分 context、
  finding 回來後不修就宣稱修了。
  同型病實錄：兩位站 5 審查者都是 Devin、與實作者同廠 => 非真正跨廠獨立。

為什麼要 role_token：
  A/B 角色靠人手動置換是一個【只會安靜失敗】的步驟 ——
  兩人各自挑角色、剛好挑同一個，報告看起來完全正常，而雙審實際上只審了一遍。
  token 由 CI 產生並拼接在訊息末尾，收件者必須原樣回填 manifest。

用法:
    python3 trigger_verifier.py --pr 12 --ticket CS06 --head-sha <sha> \
        --base-sha <sha> --role-token <tok> [--scope cs_live/]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEVIN_API = "https://api.devin.ai/v1"


def build(args, repo):
    return f"""[站 5 · mutation-verifier] {args.ticket} — PR #{args.pr}
repo: {repo}
head_sha: {args.head_sha}
base_sha: {args.base_sha}
production diff 範圍: {args.scope or '(未宣告)'}

你的**唯一產物**是一份 mutation manifest，格式見 repo 內 `ci/MUTATION_MANIFEST.md`。
散文報告沒有人讀，也不會被採信。

## 你在找什麼
**「拿掉它不會紅」的守衛。**
這個 codebase 的頭號風險是【會安靜地錯】：填錯不報錯／不會回空集合而會回一組
符號相反但看起來完全正常的損益／**沒有任何測試會紅**。
那正是規格符合性審查在定義上看不見的一格 —— 你是唯一瞄準這一類的機制。

## 硬紀律
1. 每條 mutation 必須附 `patch`（unified diff，`git apply` 套得動）。
   **沒有 patch 就沒有機械重放。**
2. `expected_red` 必填，而且要問「紅的是不是我要驗的那件事」。
   實錄：一支測試只傳一欄、缺七欄 ⇒ 守衛拿掉仍紅（紅在缺欄位），
   **測試用錯誤的理由通過**，而那個 commit 標題自稱 `make mutation bite` —— 它不 bite。
3. `survived: true` 一律要附 `repro`：一個可達輸入，使壞版與好版**都不報錯**而輸出不同。
   寫不出來就 `repro: null`（歸「待複跑」，不計分）。
   🚫 不准為了湊數把寫不出 repro 的報成存活。
4. 突變的暫時改動不算修改 —— 判準是**還原後 `git diff` 為空**。
   除此之外不改產品碼、不 push 到 main、不開 PR、不 merge。
5. **N 本身就是 finding。** 湊不到有意義的突變數就照實寫少的那個數字並說明為什麼。
   多行 `raise` 換 `pass` 是語法錯不是紅；改產生物不改來源的不算。

## 交付
把 manifest 寫到 `ci/manifests/{args.head_sha}.json`，commit 到**這個 PR 的分支**（不是 main）。
CI 會偵測到並自動重放。**你不需要通知任何人。**

## role_token（由 CI 產生 —— 原樣填進 manifest 的 producer.role_token）
{args.role_token}

🔴 如果你沒看到上面那一行 token，停下來回報「令缺 role_token」，不要開始。
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True)
    ap.add_argument("--ticket", default="")
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--base-sha", required=True)
    ap.add_argument("--role-token", required=True)
    ap.add_argument("--scope", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    key = os.environ.get("DEVIN_API_KEY", "")
    if not key and not args.dry_run:
        print("DEVIN_API_KEY 未設定", file=sys.stderr)
        return 2

    prompt = build(args, repo)
    if args.dry_run:
        print(prompt)
        return 0

    req = urllib.request.Request(
        f"{DEVIN_API}/sessions",
        data=json.dumps({"prompt": prompt, "idempotent": True}).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"觸發 verifier 失敗: {e}", file=sys.stderr)
        return 1

    sid = resp.get("session_id") or resp.get("id") or ""
    if not sid:
        print(f"回應沒有 session_id: {resp}", file=sys.stderr)
        return 1
    print(f"[verifier dispatched] {args.ticket} PR#{args.pr} -> {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
