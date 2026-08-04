# fleet-ci

站 4／站 5 自閉環的**中央排程**。一個 repo 管全部專案。

> 🔴 這個 repo 是 **public**，理由是分鐘數：private repo 的 GitHub Actions 是
> 2,000 分/月且整個帳號共用，而每 30 分一次的排程約 1,440 分/月 —— 光排程就吃掉七成，
> 站 5 的測試與突變就沒得跑。public repo 的 Actions 免費無限。
>
> ⚠️ **因此這個 repo 的 Actions log 是公開的。**
> 腳本一律 `FLEET_REDACT=1`：只印票號與計數，**不印票 title、不印派工令、不印 session id**。
> 這裡**不 checkout 任何專案原始碼** —— 測試與突變留在各專案的 private repo 跑。

## 這裡有什麼

```
.github/workflows/fleet-dispatch.yml   cron 每 30 分 ＋ 手動觸發
ci/scripts/next_dispatch.py            算前緣、派工、外科式寫回 tickets.yml
ci/scripts/fleet_poll.py               掃各 repo open PR，缺 manifest 就觸發 verifier
ci/scripts/trigger_verifier.py         觸發 mutation-verifier（觸發權在 CI，不在被審者）
ci/scripts/make_handoff.py             CI 產自陳（head/base sha、diff 範圍、baseline）
ci/scripts/replay_manifest.py          重放 manifest —— 站 5 的終判
```

## 要設的東西（一輩子只設一次）

`Settings → Secrets and variables → Actions`

| 種類 | 名稱 | 內容 |
|---|---|---|
| Secret | `DEVIN_API_KEY` | Devin API key |
| Secret | `FLEET_PAT` | fine-grained PAT，涵蓋全部專案 repo（Contents rw ＋ Pull requests rw） |
| Variable | `FLEET_REPOS` | 逗號分隔的 `owner/repo` |

🔴 **`FLEET_REPOS` 用 variable、不 commit 進檔案** —— 這個 repo 是公開的，
repo 名字進 git 就等於公開你有哪些專案。

## 專案 repo 那一側要什麼

```
tickets.yml                            票集的機器可讀版
.github/workflows/station5-verify.yml  唯一一個 workflow
ci/scripts/{replay_manifest,make_handoff}.py
ci/MUTATION_MANIFEST.md
ci/manifests/
```

🟢 **secret：零個。** verifier 由這裡觸發，key 不落地到專案 repo。

## 💰 計費

**兩張互不相干的帳單。**

- **派工給 executor**（Devin 等）是 **POST 完就走**，不等、不 poll、不 block
  ⇒ **executor 工作多久都與 GitHub 分鐘無關**。
- 🚫 **絕不可把 workflow 寫成「派工後 sleep 等回覆」** —— 那會把 executor 的工時
  **一比一轉成 Actions 分鐘**。中央改成排程輪詢，一部分理由就是這個。
- 真正吃分鐘的是**專案 repo 的 `station5-verify`**：`replay` 要逐條套上突變 patch、
  每一條都重跑一次測試套件 ⇒ 成本 ≈ **突變條數 × 測試套件長度**，約 5–15 分/PR。

⚠️ private repo 的 spending limit **預設 $0** ⇒ 額度用完 workflow **直接停，而且安靜地停**。
先到 `Settings → Billing → Spending limits` 設一個上限當保險絲。

## 新專案上線

1. 該 repo 根目錄放 `tickets.yml`（跑 `next_dispatch.py --validate` 要 0 FAIL）
2. 疊上專案側那四個檔
3. 本 repo 的 `FLEET_REPOS` 加一行
4. 把該 repo 加進 `FLEET_PAT` 的涵蓋範圍

**沒有第 5 步。不用設 secret。**

---

正典：`github-bridge` skill §J（架構與安裝）、`fleet-command` skill §站 4/5 自閉環（判準與 merge 授權）。
