# Mitosis Orchestrator (`mitosis-orchestrator`)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>次世代跨模型有絲分裂編排外掛 — 支援三大主流 AI CLI 平台</strong><br/>
  <em>Google Antigravity (<code>agy</code>) · Anthropic Claude Code · OpenAI Codex CLI (<code>0.163.0</code>)</em><br/>
  <a href="./README.md">English Documentation (README.md)</a> · <a href="./CHANGELOG.md">版本紀錄 (Changelog)</a> · <a href="./rules/AGENTS.md">編排規則 (Rules)</a>
</p>

---

## 專案簡介 — 為什麼命名為 Mitosis（有絲分裂）？

靈感源自生物學的**細胞有絲分裂（Mitosis）**，**Mitosis Orchestrator** 將您選用的**頂級前沿推理模型（Frontier Model）**定位為系統的**主核架構師（Master Nucleus）**。主核不再將昂貴的 Token 與上下文視窗消耗在單檔搜尋、原始 Log 翻找或反覆的「修改—跑測試」迴圈，而是將複雜的軟體工程任務有紀律地「分裂」並平行委派給兩類專職子細胞（Subagents）：

- **主核架構師（Primary Frontier Orchestrator）**：專注於高階架構設計、API 介面契約、跨模組重構決策、資安與並行性把關，以及最終結果的統合驗證。
- **執行子代理（`flash-worker` · `worker`）**：負責跨檔案程式碼探索、呼叫圖（Call-Graph）追蹤、外部文件查證、撰寫與執行測試，以及在明確允許清單內的局部修改（`workspace-write`）。
- **偵察子代理（`flash-lite-scout` · `explorer`）**：負責高併發、低延遲的唯讀單跳偵察——快速定位檔案與符號、執行 `grep`/`glob` 搜尋、切片檢視 `.log` 與 `transcript.jsonl`，以及單一檔案結構摘要（`read-only`）。

---

## 支援的 Primary Orchestrator 前沿模型清單

Mitosis Orchestrator 在三大平台全面支援前沿推理模型，並透過邊界正規表示式精準攔截輕量模型擔任主代理：

| 模型廠商 | 支援作為主代理的前沿模型 (Primary Orchestrator) | 自動攔截排除之輕量級模型 (Small-Tier Filter) |
| :--- | :--- | :--- |
| **OpenAI** (Codex CLI / API) | **`gpt-5.4`**、**`gpt-6.1-sol`**、**`gpt-5.6-sol`**、**`gpt-5.3-codex`**、`gpt-5.2-codex`、`gpt-5.5`、`o1`、`o3`、`o4` | `codex-mini-latest`、`gpt-5.6-luna`、`gpt-5.4-mini`、`gpt-5.3-codex-spark`、`o4-mini`、`o3-mini`、`gpt-4.1-mini` |
| **Anthropic** (Claude Code) | `Claude Opus`、`Claude Sonnet`（如 `Claude 3.7 Sonnet`、`Claude 4 Opus`） | `Claude Haiku`（`claude-3-5-haiku` 等） |
| **Google Gemini** (Antigravity `agy`) | `Gemini 4 Argon (High / Medium)`、`Gemini Pro`、`Gemini Ultra` | `gemini-3-flash`、`gemini-flash-lite`、`gemini-nano` |
| **xAI** | `Grok 3`、`Grok 4`、`grok-3`、`grok-4`、`grok-3-thinking` | `grok-3-mini`、`grok-3-mini-fast` |
| **DeepSeek** | `DeepSeek-R1`、`DeepSeek-V3`、`deepseek-reasoner`、`deepseek-chat` | `deepseek-lite` |
| **Alibaba Qwen** | `Qwen Max`、`Qwen Plus`、`Qwen 2.5 / 3`、`QwQ`（`qwq-32b`） | `qwen-lite`、`qwen-vl-lite` |

> [!NOTE]
> 內建邊界過濾器（`(?<![a-z0-9])(flash|lite|mini|nano|haiku|luna|spark)(?![a-z0-9])`），精準排除 `mini`、`flash`、`lite`、`haiku`、`nano`、`luna`、`spark` 輕量模型，同時確保 `gemini` 絕不誤判。

---

## 三平台情境路由矩陣 (Mitotic Routing Matrix v1.2.0)

| 層級 | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI (`0.163.0`) | 角色定位與職責範圍 |
| :--- | :--- | :--- | :--- | :--- |
| **主核架構師 (Master Nucleus)** | `Gemini 4 Argon (High/Medium)` / Frontier | `Claude Opus` / `Sonnet 3.7+` | **`gpt-5.4`** / **`gpt-6.1-sol`** / **`gpt-5.6-sol`** / **`gpt-5.3-codex`** / **`o3`** | 系統架構設計、多步驟任務拆解、跨模組重構、複雜邏輯與資安把關、向使用者釐清需求（`ask_question`）與最終彙整。 |
| **一級執行子代理 (Worker)** | `flash` via [`flash-worker`](./agents/flash-worker/agent.md) | `sonnet` | **`codex-mini-latest`** (預設) / **`gpt-5.4-mini`** ([`worker`](./codex-agents/worker.toml) / [`flash-worker`](./codex-agents/flash-worker.toml)) | 跨檔案探索、呼叫圖追蹤、撰寫與執行單元/整合測試、允許清單內的局部修改（`sandbox_mode = "workspace-write"`）。 |
| **二級偵察子代理 (Scout)** | `flash_lite` via [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) | `haiku` | **`gpt-5.6-luna`** (預設) / **`gpt-5.3-codex-spark`** / **`gpt-5.4-mini`** ([`explorer`](./codex-agents/explorer.toml) / [`flash-lite-scout`](./codex-agents/flash-lite-scout.toml)) | 快速檔案與符號查找、`grep`/`glob` 搜尋、`git status`/`git log` 檢查、Log 切片檢視與輕量摘要（`sandbox_mode = "read-only"`）。 |

---

## OpenAI Codex CLI (`0.163.0`) 整合與自動掛載

Mitosis Orchestrator 原生相容 OpenAI Codex CLI，提供一鍵自動掛載腳本：

```bash
python scripts/setup-codex.py
```

### 自動掛載動作說明
1. **自動備份**：將現有 `~/.codex/config.toml` 複製備份至 `~/.codex/config.toml.bak`。
2. **同步規則**：將 `rules/AGENTS.md` 同步複製至 `~/.codex/AGENTS.md`。
3. **安裝角色**：將 `codex-agents/*.toml` 角色設定安裝至 `~/.codex/agents/`：
   - `explorer.toml` 與 `flash-lite-scout.toml`（`model = "gpt-5.6-luna"`、`model_reasoning_effort = "low"`、`sandbox_mode = "read-only"`）
   - `worker.toml` 與 `flash-worker.toml`（`model = "codex-mini-latest"`、`model_reasoning_effort = "medium"`、`sandbox_mode = "workspace-write"`）
4. **注入設定**：非破壞式追加 `[profiles.mitosis-scout]`、`[profiles.mitosis-worker]` 與 `[mcp_servers.mitosis]` 至 `~/.codex/config.toml`。
5. **完整性驗證**：自動校驗所有 TOML 格式正確性並確認檔案存在。

---

## 核心組件與安全防護機制

### 1. 生命週期強制守衛 ([`hooks.json`](./hooks.json) 與 [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py))
完全零外部依賴（僅使用 Python 3.8+ 標準函式庫），於每個代理生命週期自動執行以下把關：

| Hook 事件 | 防護機制與行為說明 |
| :--- | :--- |
| `SessionStart` | 讀取設定檔，若主模型非支援的前沿模型則發出**警告**並注入 Mitosis 編排政策；僅在啟用 `AGY_MITOSIS_PIN_SETTINGS=1` 時自動改寫設定檔。 |
| `PreInvocation` | 每回合即時檢查即時模型；若使用者切換至非前沿模型，每回合注入一次提醒。同時重設單回合編輯計數（`turn_edited_files`）與連續失敗計數（`consecutive_failures`）。 |
| `PreToolUse` · `invoke_subagent` | 自動判定子代理應綁定執行或偵察層級。強訊號（測試、除錯、修改動詞）一律優先於查找關鍵字；若偵察子代理被指派修改任務，會自動升級為執行子代理。 |
| `PreToolUse` · `send_message` | 攔截對偵察子代理發送程式碼修改或超過 3 個編號步驟的複雜追加指令。 |
| `PreToolUse` · `run_workflow` | 攔截工作流腳本中未明確指定子代理模型層級的呼叫。 |
| `PreToolUse` · `define_subagent` | 強制將 `enable_subagent_tools` 設為 `false`，防止子代理無限巢狀衍生子代理。 |
| `PreToolUse` · **情境整潔守衛 (Context Hygiene)** | 攔截主代理直接讀取 `.log` 或 `transcript.jsonl` 原始大檔，強制委派偵察子代理進行切片摘要，保護主代理上下文視窗不被巨量日誌污染。 |
| `PreToolUse` · **快捷修改預算 (Fast-Path Budget)** | 限制主代理單回合直接修改工作區檔案上限為 `3` 個（可由 `AGY_MITOSIS_MAX_DIRECT_EDITS` 調整），並禁止主代理在同回合直接修改檔案後執行測試框架。 |
| `PreToolUse` · **兩次失敗熔斷器 (Circuit Breaker)** | 當指令或子代理調用連續失敗達 `2` 次時立即觸發熔斷，阻止無效重試迴圈並要求回報阻礙原因。 |
| `PreToolUse` · **工作區範圍守衛 (Workspace Guard)** | 選用功能（`AGY_MITOSIS_AUTO_ALLOW=1`）：工作區與 Artifact 目錄內的檔案讀寫及安全命令自動放行（`allow`）；跨出工作區、提權（`sudo`）或危險指令則轉為詢問（`ask`）。未啟用時一律回傳明確的 `allow` 通行。 |
| `PostToolUse` | 自動配對新建子代理中繼資料，並更新連續失敗計數。 |
| `Stop` | 當回合進入完全閒置且仍有存活的分支工作樹時，提醒主代理先合併或捨棄變更並清理。 |

### 2. 有界四標頭子代理回報合約 (Bounded 4-Header Report Contract)
所有偵察子代理與執行子代理皆強制採用極簡四標頭格式回報（總長 $\le 1,600$ 字元、`NOTES:` 下最多 10 行清單條目，且當狀態為 `BLOCKED` 或 `FAILED` 時必須附上 `file://.../*.log` 錯誤日誌連結）：

```markdown
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <以逗號分隔的檔案路徑或 NONE>
VERIFICATION: <一句話說明執行的驗證命令與結果>
NOTES: <精簡摘要（可含最多 10 行 `- ` 清單）；若 BLOCKED/FAILED 須含 file:// log URI>
```

### 3. `/orchestrate` 四階段編排技能 ([`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md))
提供結構化的 `/orchestrate` 四階段工作流：
1. **階段一：平行偵察 (Scout / Explorer Fan-Out)** — 派發唯讀偵察子代理進行高速檔案與符號探索。
2. **階段二：架構設計與拆解 (Primary Master Nucleus)** — 由前沿主模型彙整偵察結果、制定架構介面並切分獨立工作單元。
3. **階段三：範圍執行與測試 (Workers)** — 派發執行子代理於指定檔案清單內完成實作與單元測試驗證。
4. **階段四：統合驗證與分支清理 (Primary Master Nucleus)** — 檢視差異、完成最終驗證並清理工作樹。

---

## 環境變數設定 (`AGY_MITOSIS_*` / `CODEX_MITOSIS_*`)

全面採用 `AGY_MITOSIS_*` 與 `CODEX_MITOSIS_*` 環境變數，並向下相容舊版 `AGY_ARGON_*` 變數：

| 環境變數 | 預設值 | 功能說明 |
| :--- | :--- | :--- |
| `AGY_MITOSIS_PIN_SETTINGS` | `0` | 設為 `1` 時，於 Session 啟動時自動將設定檔改寫為指定的前沿主模型。 |
| `AGY_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` | 當啟用自動綁定時的目標前沿主模型名稱（例如 `gpt-5.4`、`gpt-6.1-sol`、`Claude 3.7 Sonnet`、`o3`）。 |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` | `0` | 設為 `1` 可關閉所有非前沿主模型的警告訊息。 |
| `AGY_MITOSIS_AUTO_ALLOW` | `0` | 設為 `1` 時啟用互動模式下的工作區範圍自動放行守衛。 |
| `AGY_MITOSIS_MAX_DIRECT_EDITS` | `3` | 主代理單一回合可直接修改的不同工作區檔案數量上限。 |
| `AGY_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | 設為 `1` 時允許主代理直接讀取 `.log` 與 `transcript.jsonl` 原始檔。 |
| `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | 設為 `1` 時允許主代理繞過單回合直接修改數量限制與「修改後跑測試」限制。 |
| `AGY_MITOSIS_CIRCUIT_BREAKER` | `1` | 設為 `0` 時停用連續 2 次失敗自動熔斷機制。 |
| `AGY_MITOSIS_DEBUG` | `0` | 設為 `1` 時於 `<artifactDirectoryPath>/mitosis_orchestrator_debug.log` 記錄每次 Hook 呼叫的診斷 JSON。 |

---

## 安裝與設定方式

### 平台一：Google Antigravity (`agy`)
```bash
agy plugin install ~/mitosis-orchestrator
# 或直接 clone 至全域 plugins 目錄
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
```

### 平台二：Anthropic Claude Code
```bash
# 透過 .claude-plugin/plugin.json 安裝
claude plugin add ~/mitosis-orchestrator
```

### 平台三：OpenAI Codex CLI (`0.163.0`)
```bash
# 一鍵自動掛載至 ~/.codex（安裝角色、設定檔與編排規則）
python scripts/setup-codex.py
```

---

## 開發與測試

執行完整單元測試與插件結構驗證：

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```

詳細版本更新紀錄請參閱 [CHANGELOG.md](./CHANGELOG.md)。

---

## 授權條款

[MIT](./LICENSE) © 2026 ivanusto
