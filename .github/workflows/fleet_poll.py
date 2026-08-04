#!/usr/bin/env python3
"""中央輪詢 —— 一個 public 的 `fleet-ci` repo 管全部專案的站 4/5。

為什麼是輪詢而不是事件驅動（2026-08-05 定案，Glenn）：
  舊架構要求【每個專案 repo】各放 8 個 CI 檔 ＋ 各設一次 `DEVIN_API_KEY`。
  ⇒ 檔案複製與 secret 複製兩個病根，每開一個新專案就再犯一次。
  · 檔案複製 → reusable workflow 解得掉。
  · **secret 複製解不掉** —— GitHub 個人帳號沒有跨 repo 的 secret，
    而 Free 方案的 org secret **不能給 private repo 用**
    （官方社群原話：「Organization secrets cannot be used by private repositories with your plan.」）
  ⇒ 反過來由中央拉：secret 只存在 `fleet-ci` 一個地方，**專案 repo 零 secret**。

為什麼 `fleet-ci` 是 public：
  private repo 的 Actions 分鐘是 **2,000/月、整個帳號共用**；public repo **免費無限**。
  每 30 分一次的排程 ≈ 1,440 分/月，**光排程就吃掉七成額度**，站 5 就沒得跑。
  ⇒ 編排放 public（不 checkout 任何專案原始碼），測試與突變留在專案 repo（private）。
  🔴 **因此本檔的 log 是公開的** —— 一律 `FLEET_REDACT=1`，只印票號與計數。

用法:
    python3 fleet_poll.py --repos "owner/a,owner/b" [--max-concurrent 3] [--dry-run]

環境變數:
    FLEET_PAT       必要。fine-grained PAT，涵蓋全部專案 repo（contents:rw, pull_requests:rw）
    DEVIN_API_KEY   必要（除非 --dry-run）
    FLEET_REDACT    建議固定 1

Exit: 0 全部正常, 1 有 repo 失敗（其餘照跑完）, 2 用法/環境錯誤
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

GH = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))


def gh(path, pat, method="GET"):
    req = urllib.request.Request(
        f"{GH}{path}",
        headers={"Authorization": f"Bearer {pat}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None


def open_prs(repo, pat):
    st, body = gh(f"/repos/{repo}/pulls?state=open&per_page=50", pat)
    if st != 200:
        raise RuntimeError(f"list PRs HTTP {st}")
    return body or []


def has_manifest(repo, head_sha, pat):
    """判準是【檔案在不在】，不是「有沒有人被叫來過」。

    session id 只能證明有人被叫來過，證明不了他審了什麼 ——
    所以站 5 的終判一直是重放 manifest，這裡只負責「還沒有 manifest 就去要一份」。
    """
    st, _ = gh(f"/repos/{repo}/contents/ci/manifests/{head_sha}.md", pat)
    return st == 200


def run(cmd, env=None):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default=os.environ.get("FLEET_REPOS", ""))
    ap.add_argument("--max-concurrent", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repos = [r.strip() for r in args.repos.replace("\n", ",").split(",")
             if r.strip()]
    if not repos:
        print("FLEET_REPOS 沒有內容 ⇒ 沒有專案要輪詢。"
              "在 fleet-ci 的 Settings → Secrets and variables → Actions → Variables "
              "新增 FLEET_REPOS（逗號分隔的 owner/repo）。", file=sys.stderr)
        return 2

    pat = os.environ.get("FLEET_PAT", "")
    if not pat:
        print("FLEET_PAT 未設定", file=sys.stderr)
        return 2
    if not os.environ.get("DEVIN_API_KEY") and not args.dry_run:
        print("DEVIN_API_KEY 未設定", file=sys.stderr)
        return 2

    bad = []
    for repo in repos:
        print(f"::group::{repo}")
        try:
            prs = open_prs(repo, pat)
            print(f"[{repo}] open PR {len(prs)} 顆")
            for pr in prs:
                head = (pr.get("head") or {}).get("sha", "")
                num = pr.get("number")
                # 🚫 PR title 不印 —— 公開 log。
                if has_manifest(repo, head, pat):
                    print(f"  PR#{num} 已有 manifest ⇒ 交給該 repo 的 replay 判生死")
                    continue
                print(f"  PR#{num} 缺 manifest ⇒ 觸發 verifier")
                if args.dry_run:
                    continue
                base = (pr.get("base") or {}).get("sha", "")
                rc, out = run([sys.executable,
                               os.path.join(HERE, "trigger_verifier.py"),
                               "--pr", str(num), "--head-sha", head,
                               "--base-sha", base,
                               "--role-token", f"{repo}:{head[:12]}"],
                              env={"GITHUB_REPOSITORY": repo})
                if rc != 0:
                    # 🚫 不回吐 out —— 它可能夾帶票內容
                    bad.append(f"{repo}#{num}: trigger_verifier rc={rc}")
                    print(f"  🔴 PR#{num} 觸發失敗 rc={rc}")
        except (RuntimeError, urllib.error.URLError, TimeoutError) as e:
            bad.append(f"{repo}: {type(e).__name__}")
            print(f"[{repo}] 🔴 {type(e).__name__}")
        finally:
            print("::endgroup::")

    if bad:
        print("失敗：\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
