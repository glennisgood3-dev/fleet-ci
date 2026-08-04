#!/usr/bin/env python3
"""依賴解除後自動續派 —— 站 4 的「一次全派」在 CI 這一端的實作。

觸發時機：push 到 main（= 有 PR merge 了）。
做的事：把剛 merge 的票標 done -> 重算前緣 -> 把【全部】新解鎖的票一次派給 Devin。

不是一張一張派、不等 Commander 點頭。Commander 在站 4/5 只做兩件事：派工、處理 FAIL。

用法:
    python3 next_dispatch.py --tickets tickets.yml --merged-ticket CS06 \
        [--max-concurrent 3] [--dry-run]

環境變數:
    DEVIN_API_KEY   必要（GitHub repo secret；絕不寫進 log 或 commit）

Exit: 0 正常（含「沒有票可派」）, 1 派工失敗, 2 用法/環境錯誤
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:
    print("需要 pyyaml: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

DEVIN_API = "https://api.devin.ai/v1"

# 🔴 v0.4.0：中央 `fleet-ci` 是 **public repo**（為了拿到免費且無限的 Actions 分鐘）
#    ⇒ **它的 Actions log 是公開的**。票的 title、派工令全文、Devin session id
#    落進 log 就等於公開專案的規格摘要與契約。
#    ⇒ `FLEET_REDACT=1`（中央模式一律開）時，只印票號與計數，不印任何自由文字。
#    ⚠️ 派工令本文【任何模式都不印】—— 它從來沒有進 log 的理由。
REDACT = os.environ.get("FLEET_REDACT", "") == "1"


def rd(text, keep=0):
    """公開 log 模式下把自由文字換成長度標記。keep>0 保留前 N 個字元。"""
    if not REDACT:
        return text
    t = str(text)
    return (t[:keep] + "…") if keep else f"<redacted:{len(t)}c>"

DONE = {"done", "merged"}

# 狀態詞彙與 fleet_state 的 issues 欄對齊，不得自創。
# v0.3.2 更正：舊範例寫 pending / in_review，與正典的 todo / review 不一致
# => 兩份真相源會漂。舊值仍可讀，validate 會 WARN。
CANON = {"todo", "dispatched", "running", "review", "done", "blocked"}
ALIAS = {"pending": "todo", "in_review": "review", "merged": "done"}
OURS = {"commander", "executor", "devin", "ci", "自己", "我方"}


def norm_status(t):
    return ALIAS.get((t.get("status") or "todo").strip(),
                     (t.get("status") or "todo").strip())


# 🔴 v0.3.4：**測試指令不得寫死。**
#   舊版 `build_prompt` 把 `pytest` 直接印進派工令的「判準」段（兩處：突變步驟與判準表）。
#   實錄：`Prematch_LoL` 是 **Node 專案** ⇒ 那條判準對它是空的。
#   executor 跑不出 pytest，卻仍會照令回報「判準過了」—— **安靜地成功是最糟的失敗。**
#   ⇒ 指令一律從宣告來（票級 > 專案級），**沒有宣告就 fail-closed 不派**。
#   ⚠️ 這是刻意擋門：派一封帶錯測試指令的令比不派更貴 —— 換回來的是一份看起來綠的假證據。
def resolve_test_cmd(t, data):
    return ((t or {}).get("test_cmd")
            or (data or {}).get("test_cmd") or "").strip()


def _sect(title, body):
    """有值才產出一段 —— 避免派工令長出一排 `(無)`，那會訓練 executor 略讀。"""
    if isinstance(body, (list, tuple)):
        items = [str(x).strip() for x in body if str(x).strip()]
        body = "\n".join(f"  - {x}" for x in items)
    else:
        body = str(body or "").strip()
    return f"\n## {title}\n{body}\n" if body else ""


def validate(data):
    """把常見的【導出】缺陷變成 FAIL，不用人工看出來。

    只檢查導出衛生，**不檢查票拆得好不好**（顆粒度、切片方式）——
    那是站 3 三軸審查的事。一個會對顆粒度打分的 linter
    會讓人為了過 lint 而改拆票方式，那是 Goodhart，比它治的病更糟。
    """
    fails, warns = [], []
    tickets = data.get("tickets") or []
    ids = {t.get("id") for t in tickets}
    ext_seen = {}

    for t in tickets:
        tid = t.get("id", "<no id>")
        raw = (t.get("status") or "todo").strip()
        if raw in ALIAS:
            warns.append(f"{tid}: status={raw!r} 是舊詞彙 => 改用 {ALIAS[raw]!r}（與正典對齊）")
        elif raw not in CANON:
            fails.append(f"{tid}: status={raw!r} 不在 {sorted(CANON)}")

        if (t.get("title") or "").rstrip().endswith(("＋", "+", "／", "/", "、", ",")):
            fails.append(f"{tid}: title 結尾像被截斷 => {rd(t['title'], 12)!r}")

        if norm_status(t) not in DONE and not (t.get("scope") or []):
            fails.append(f"{tid}: scope 為空 => 站 5「diff 在票範圍內」判準無輸入")

        # 🔴 測試指令未宣告 ⇒ 不派（見 resolve_test_cmd 檔內註解）
        if norm_status(t) not in DONE and not resolve_test_cmd(t, data):
            fails.append(
                f"{tid}: 沒有 test_cmd => 派工令的測試判準會是空的。"
                f" 在 tickets.yml 頂層加一行 `test_cmd: \"<跑測試的指令>\"`"
                f"（例：`npm test` / `pytest -q` / `go test ./...`），"
                f" 單票不同再在該票上覆寫。🚫 不得預設 pytest —— 本 repo 不一定是 Python")

        for b in (t.get("blockers") or []):
            owner = str(b.get("owner", "")).strip().lower()
            what = str(b.get("what", ""))
            if owner in OURS:
                fails.append(f"{tid}: blockers[{b.get('id')}] owner={b.get('owner')!r} 是我方"
                             f" => 那不是 blocker，是【還沒做完】，用 status: running 表達")
            if b.get("resolved") and what.lstrip().startswith(("\U0001F7E2", "\u2705")):
                fails.append(f"{tid}: blockers[{b.get('id')}] 是好消息不是 blocker"
                             f" => 不要倒看板敘述進來")
            if len(what) > 160:
                warns.append(f"{tid}: blockers[{b.get('id')}].what {len(what)} 字"
                             f" => 寫一句「什麼擋住它」就好")

        for b in (t.get("external_blockers") or []):
            ext_seen.setdefault(b.get("id"), []).append(tid)

        for e in (t.get("depends_on") or []):
            e = {"id": e, "type": "artifact"} if isinstance(e, str) else e
            if e.get("id") not in ids:
                fails.append(f"{tid}: depends_on 指向不存在的票 {e.get('id')!r}")
            if e.get("type") == "contract" and not e.get("contract_source"):
                fails.append(f"{tid}: depends_on[{e.get('id')}] 標 contract 但無 contract_source"
                             f" => 只有附來源的 contract 邊可以被 mock 消除")
            if e.get("type") not in (None, "contract", "data-shape", "measurement", "artifact"):
                fails.append(f"{tid}: depends_on[{e.get('id')}].type={e.get('type')!r} 不合法")

    for eid, holders in ext_seen.items():
        if len(holders) >= 3:
            fails.append(f"external_blocker {eid!r} 重複出現在 {len(holders)} 張票"
                         f"（{holders[:4]}…）=> 那是專案級，提升到 project_blockers")

    if not (data.get("assumptions") or []):
        warns.append("assumptions 缺席 => 站 3 出口判準 E3 未做")
    for a in (data.get("assumptions") or []):
        if a.get("grade") == "猜":
            for c in (a.get("consumed_by") or []):
                fails.append(f"{c}: 契約建立在標『猜』的假設 {a.get('id')} 上 => E3 擋門")
    return fails, warns



def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# 🔴 v0.4.0 重寫（實錄 2026-08-05）：舊版 `save()` 用 `yaml.safe_dump` 整份重寫，
#    **把 tickets.yml 的註解全部洗掉**（`tickets.example.yml` 的欄位語意【全在註解裡】
#    ⇒ CI 第一次跑就把說明書洗掉）。更糟的是 `auto-dispatch.yml` 用
#    `git diff --quiet -- tickets.yml` 判斷要不要 commit ⇒ 註解被洗 = 永遠有 diff
#    ⇒ **每次 merge 都多一個內容為空的「前緣更新」commit。**
#    ⇒ 改成只改動真正變了的那幾行（status / devin_session），其餘位元組不動。
def apply_updates(text, updates):
    """就地改寫 `- id: X` 區塊裡的 status / devin_session，其餘一律不動。

    updates = {ticket_id: {"status": "...", "devin_session": "..."}}
    回傳 (新文字, 實際改了幾個欄位)。**找不到的欄位用插入，不用重寫整份。**
    """
    lines = text.split("\n")
    # 🔴 2026-08-05 修（實測抓到）：只認 **頂層 `tickets:` 底下**的 `- id:`。
    #    第一版對全檔掃 `- id:`，而 block style 的 `depends_on:` 長這樣：
    #        depends_on:
    #        - id: LOL-02
    #          type: artifact
    #    ⇒ **依賴邊被當成票**，於是 `LOL-04a` 出現在 5 張下游票的 depends_on 裡，
    #      每一處都被插入一行 `status:` ⇒ 一次改動報 **12 個欄位**（正確答案是 2）。
    #    這會把 tickets.yml 寫壞，而且壞在「看起來只是多了幾行」。
    tickets_at = next((i for i, ln in enumerate(lines)
                       if re.match(r"^tickets:\s*$", ln)), None)
    starts = []
    if tickets_at is not None:
        item_indent = None
        for i in range(tickets_at + 1, len(lines)):
            ln = lines[i]
            # ⚠️ 結束條件不能只看「非空白開頭」—— `yaml.safe_dump` 產出的清單項目
            #    本身就在第 0 欄（`- id: LOL-01`），那樣會在第一張票就 break。
            if re.match(r"^[^\s-]", ln):
                break               # 下一個頂層 key ⇒ tickets 區段結束
            m = re.match(r"^(\s*)-\s+id:\s*[\"\']?([A-Za-z0-9_.-]+)", ln)
            if not m:
                continue
            indent = len(m.group(1))
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:   # 只收與第一張票同縮排的
                starts.append((i, indent, m.group(2)))
    changed = 0
    for n, (i, indent, tid) in enumerate(starts):
        if tid not in updates:
            continue
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        field_indent = " " * (indent + 2)
        for key, val in updates[tid].items():
            pat = re.compile(r"^\s*" + re.escape(key) + r":\s*.*$")
            for j in range(i, end):
                if pat.match(lines[j]):
                    new = f"{field_indent}{key}: {val}"
                    if lines[j] != new:
                        lines[j] = new
                        changed += 1
                    break
            else:
                lines.insert(i + 1, f"{field_indent}{key}: {val}")
                changed += 1
                starts = [(k + 1 if k > i else k, ind, t)
                          for (k, ind, t) in starts]
                end += 1
    return "\n".join(lines), changed


def save(path, original_text, updates, dry):
    """🔴 `--dry-run` 一律不寫檔。

    舊版 `main()` 兩處【無條件】呼叫 save()，含「本輪沒有新解鎖的票」那條 return
    路徑與 `--dry-run` —— 一個宣稱「只印不送」的旗標卻會改檔，是最容易被信任的那種缺陷。
    """
    new_text, changed = apply_updates(original_text, updates)
    if dry:
        if changed:
            print(f"[dry-run] 會改 {changed} 個欄位（實際未寫檔）")
        return 0
    if not changed:
        return 0
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return changed


def frontier(tickets, max_concurrent, data_station_cap=5):
    """可派集合 = pending 且上游全 done 且沒有未解的外部 blocker。

    只有 contract 型別的邊允許被 mock 消除，而那要求該邊已附 contract_source
    （spec 行號或 decision ID）。data-shape / measurement 邊不得 mock 掉 ——
    實錄：以為知道資料長什麼樣、拿到才發現不是函數的，已經有四次。
    """
    by_id = {t["id"]: t for t in tickets}
    in_flight = sum(1 for t in tickets if norm_status(t) == "dispatched")
    ready, blocked_reasons = [], {}

    for t in tickets:
        if norm_status(t) != "todo":
            continue
        tid = t["id"]

        # --- 以下四條移植自 §AC / auto_continue.py ---
        # 它們不是上限，是正確性。派工權統一在本腳本之後，那四條必須跟過來。

        # (1) 有 blocker 不派
        own = [b for b in (t.get("blockers") or []) if not b.get("resolved")]
        if own:
            blocked_reasons[tid] = f"blocker: {[b.get('id') for b in own]}"
            continue

        # (2) 站序不可跳
        if int(t.get("station", 4)) > int(data_station_cap):
            blocked_reasons[tid] = f"站序: 票在站 {t.get('station')}，專案在站 {data_station_cap}"
            continue

        # (3) gateExhausted —— 送審上限（L29）已達 ⇒ 不得再送第 N+1 輪
        if t.get("gate_exhausted") or int(t.get("gate_rounds", 0)) >= int(
                t.get("gate_limit", 3)):
            blocked_reasons[tid] = (
                f"gateExhausted: {t.get('gate_rounds', 0)}/{t.get('gate_limit', 3)} "
                f"⇒ 走 ship_anyway 或停手升級，不得自動再派")
            continue

        # (4) 外部 blocker
        ext = [b for b in (t.get("external_blockers") or [])
               if not b.get("resolved")]
        if ext:
            blocked_reasons[tid] = f"外部: {[b.get('id') for b in ext]}"
            continue

        unmet = []
        for edge in (t.get("depends_on") or []):
            # 邊可以是 "CS04" 或 {id: CS04, type: contract, contract_source: "..."}
            if isinstance(edge, str):
                edge = {"id": edge, "type": "artifact"}
            up = by_id.get(edge["id"])
            if up is None:
                blocked_reasons[tid] = f"指向不存在的票 {edge['id']}"
                unmet.append(edge["id"])
                continue
            if norm_status(up) in DONE:
                continue
            # contract 邊 + 已凍結來源 => 可對契約寫，不必等上游實作
            if edge.get("type") == "contract" and edge.get("contract_source"):
                continue
            unmet.append(edge["id"])

        if unmet:
            blocked_reasons[tid] = f"上游未完成: {unmet}"
            continue

        # (5) blocker 跨站繼承 —— 實錄：某批票站 5 沒有自己的 blocker，
        #     照字面會去派審查，而那份程式即將被上游令重寫。
        inherited = []
        for edge in (t.get("depends_on") or []):
            eid = edge if isinstance(edge, str) else edge.get("id")
            up = by_id.get(eid) or {}
            if [b for b in (up.get("blockers") or []) if not b.get("resolved")]:
                inherited.append(eid)
        if inherited:
            blocked_reasons[tid] = f"blocker 跨站繼承自: {inherited}"
            continue

        ready.append(t)

    slots = max(0, max_concurrent - in_flight)
    if len(ready) > slots:
        # 具名截斷 —— 靜默 truncate 會被讀成「全部派了」
        print(f"[note] 可派 {len(ready)} 張，並行上限 {max_concurrent}，"
              f"在飛 {in_flight} ⇒ 本輪派 {slots} 張，"
              f"其餘留待下一次 merge: {[t['id'] for t in ready[slots:]]}")
        ready = ready[:slots]
    return ready, blocked_reasons


def build_prompt(t, repo, manifest_doc, data=None):
    """把票【整張】翻成派工令。

    🔴 v0.3.4 修兩個缺陷（實錄）：
      (9) 判準寫死 `pytest` ⇒ Node 專案的測試判準是空的。改成 `resolve_test_cmd`。
      (10) 票上其實有 `red_plan`／`status_note`／`source_md`／`document` 四個欄位，
           **舊版一個都沒讀** ⇒ executor 拿不到「先紅要紅在哪」與「一級來源在哪」，
           只好自己猜，於是 TDD 的紅那一段變成事後補寫的裝飾。
           ⚠️ 具名代價：派工令變長。但長一點的令換掉一輪 rework 是划算的
           （現況每張 merge 票平均 2 輪站 5 ＋ 2.7 封補丁令）。
    """
    data = data or {}
    scope = ", ".join(t.get("scope") or []) or "(未宣告)"
    # 🔴 2026-08-05 新增 `branch`（實測抓到，A-9 ①自審）：
    #    舊版把「從 origin/main 開 <ticket>-impl」寫死。
    #    對【續作既有 PR】的票（`status_note` 明說 PR#N 開著、要修存活突變）
    #    這句話與現實直接矛盾 —— executor 會從 main 重開一條分支，
    #    **把既有 PR 的工作整個丟掉，而且它自己不會覺得有問題。**
    #    ⇒ 票可以宣告 `branch:`（沿用既有分支）；沒宣告才用預設。
    branch_line = (f"分支: **沿用既有分支 `{t['branch']}`**（不要從 main 重開）"
                   if t.get("branch") else
                   f"分支: 從 origin/main 開 `{t['id'].lower()}-impl`")
    contracts = "\n".join(f"  - {c}" for c in (t.get("contract_refs") or [])) or "  (無)"
    criteria = "\n".join(f"  - [ ] {c}" for c in (t.get("acceptance") or [])) or "  (見票)"
    test_cmd = resolve_test_cmd(t, data)      # validate 已 fail-closed，這裡必有值

    # 一級來源：票集 markdown / 規格文件。票級優先，其次專案級。
    src = t.get("source_md") or data.get("source_md") or ""
    doc = t.get("document") or data.get("document") or ""
    sources = [x for x in (src, doc) if str(x).strip()]

    return f"""[站 4 自閉環派工] {t['id']} — {t.get('title', '')}
repo: {repo}
{branch_line}

## 要做出來的行為
{str(t.get('what_to_build') or '(見票集)').strip()}
{_sect("一級來源（有疑義以它為準，不要以本封令為準）", sources)}{_sect("目前狀態（接手前先讀）", t.get("status_note"))}
## 凍結契約（不得自行更改；要改走 G3 流程）
{contracts}

## production diff 允許範圍
{scope}

## 驗收條件
{criteria}
{_sect("先紅計畫（red_plan）—— 這是【指定】的紅，不是建議", t.get("red_plan"))}
## 你要自己閉環，不要停下來等裁決
1. 實作，TDD：**先紅後綠**，兩段證據都要留在 commit 歷史裡。
   {"上面的『先紅計畫』是指定的紅：要照它先寫出會紅的測試，紅了才准開始寫實作。"
     if str(t.get("red_plan") or "").strip()
     else "本票沒有指定 red_plan ⇒ 由你自己宣告先紅在哪，並把宣告寫進第一個 commit message。"}
2. **自己跑紅綠燈突變**：對每個 public 輸出函式，至少做「拿掉守衛／改邊界／吞例外」三類。
   每條記下：檔案:行、patch(unified diff)、預期紅在哪幾支測試、`{test_cmd}` 的實際輸出。
3. 存活的突變**自己補測試修掉**；修不掉的必須寫出 repro
   （一個可達輸入，使壞版與好版**都不報錯**而輸出不同）。
4. 全部判準過了才 push。**不要回報中間狀態、不要問可不可以繼續。**

## 判準（機器跑，不是人讀）
- `{test_cmd}` 綠（🔴 這只是前置過濾器，不是終判 —— 本專案已否證四次，
  最刺眼的一次是把 `return` 包成 `try/except` 吞掉，全綠）
- production diff 在上面宣告的範圍內
- 沒有 category=silent-failure 且存活且有 repro 的突變
- manifest 格式見 repo 內 `{manifest_doc}`

## 唯一該停下來問的情況
撞到**契約本身是錯的**（照契約寫會產生一個必然錯誤的行為）。
那時停手、把問題寫清楚、不要自行改共用型別。其餘一切自己收斂。
"""


def dispatch(prompt, key, dry):
    if dry:
        return {"session_id": "dry-run", "result": "sent"}
    req = urllib.request.Request(
        f"{DEVIN_API}/sessions",
        data=json.dumps({"prompt": prompt, "idempotent": True}).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"HTTP {r.status}")
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickets", default="tickets.yml")
    ap.add_argument("--merged-ticket", default="")
    ap.add_argument("--max-concurrent", type=int, default=3)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--manifest-doc",
                    default="ci/MUTATION_MANIFEST.md")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true",
                    help="只檢查導出衛生，不派工。（派工路徑本來就會先跑，這個旗標是給人單獨檢查用）")
    args = ap.parse_args()

    if args.validate:
        data = load(args.tickets)
        fails, warns = validate(data)
        for w in warns:
            print("WARN", w)
        for x in fails:
            print("FAIL", x)
        print(f"--- {len(data.get('tickets') or [])} 張票 · "
              f"FAIL {len(fails)} · WARN {len(warns)}")
        return 1 if fails else 0

    key = os.environ.get("DEVIN_API_KEY", "")
    if not key and not args.dry_run:
        print("DEVIN_API_KEY 未設定", file=sys.stderr)
        return 2

    data = load(args.tickets)
    # 原文留著 —— save() 只改動變了的那幾行，不重寫整份（保留註解，見 apply_updates）
    with open(args.tickets, encoding="utf-8") as _f:
        original_text = _f.read()
    updates = {}
    tickets = data.get("tickets") or []
    if not tickets:
        print("tickets.yml 沒有票")
        return 0

    # 🔴 派工前【一律】先驗，不是選配。
    # 靠人記得跑的檢查等於沒有檢查 —— 本 plugin 已經記過五次
    # 「被描述成自動、實際沒有人在跑」的機制。這一條不留第六個。
    # fail-closed：壞掉的 tickets.yml 不得派工（前緣算錯 = 白工，比不派更貴）。
    fails, warns = validate(data)
    for w in warns:
        print("WARN", w)
    if fails:
        for x in fails:
            print("FAIL", x)
        print(f"🔴 tickets.yml 有 {len(fails)} 條 FAIL ⇒ 本輪不派工。"
              f"修完再 push，或先跑 --validate 看完整清單。", file=sys.stderr)
        return 1

    if args.merged_ticket:
        for t in tickets:
            if t["id"] == args.merged_ticket:
                t["status"] = "done"
                updates.setdefault(t["id"], {})["status"] = "done"
                print(f"[merged] {t['id']} -> done")

    ready, blocked = frontier(tickets, args.max_concurrent,
                              data.get('station_cap', 5))

    if not ready:
        print("本輪沒有新解鎖的票。擋住的原因：")
        for tid, why in sorted(blocked.items()):
            print(f"  {tid}: {why}")
        save(args.tickets, original_text, updates, args.dry_run)
        return 0

    failures = []
    for t in ready:
        prompt = build_prompt(t, args.repo, args.manifest_doc, data)
        try:
            resp = dispatch(prompt, key, args.dry_run)
        except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
            failures.append(f"{t['id']}: {rd(e, 60)}")
            continue
        sid = resp.get("session_id") or resp.get("id") or ""
        if not sid:
            failures.append(f"{t['id']}: 回應沒有 session_id: {rd(resp, 40)}")
            continue
        t["status"] = "dispatched"
        t["devin_session"] = sid
        updates.setdefault(t["id"], {}).update(
            {"status": "dispatched", "devin_session": sid})
        # session id 在公開 log 裡遮掉 —— 它是可以直接開的 Devin 會話網址
        print(f"[dispatched] {t['id']} -> {rd(sid, 12)}")

    save(args.tickets, original_text, updates, args.dry_run)

    if failures:
        print("派工失敗：\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
