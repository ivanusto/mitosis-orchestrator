# Mitosis Orchestrator (`mitosis-orchestrator`)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>專為 Google Antigravity (<code>agy</code>) 打造的次世代跨模型有絲分裂編排外掛</strong><br/>
  <a href="./README.md">English Documentation (README.md)</a> · <a href="./CHANGELOG.md">版本紀錄 (Changelog)</a> · <a href="./rules/AGENTS.md">編排規則 (Rules)</a>
</p>

---

## 專案簡介 — 為什麼命名為 Mitosis（有絲分裂）？

靈感源自生物學的**細胞有絲分裂（Mitosis）**，**Mitosis Orchestrator** 將您選用的**頂級前沿推理模型（Frontier Model）**定位為系統的**主核架構師（Master Nucleus）**。主核不再將昂貴的 Token 與上下文視窗消耗在單檔搜尋、原始 Log 翻找或反覆的「修改—跑測試」迴圈，而是將複雜的軟體工程任務有紀律地「分裂」並平行委派給兩類專職子細胞（Subagents）：

- **主核架構師（Primary Frontier Orchestrator）**：專注於高階架構設計、API 介面契約、跨模組重構決策、資安與並行性把關，以及最終結果的統合驗證。
- **一級執行子代理（`flash-worker` · `flash`）**：負責跨檔案程式碼探索、呼叫圖（Call-Graph）追蹤、網頁與技術文件查證、撰寫與執行測試，以及在明確允許清單（Allowlist）內的局部程式碼修改。
- **二級偵察子代理（`flash-lite-scout` · `flash_lite`）**：負責高併發、低延遲的唯讀單跳偵察——快速定位檔案與符號、執行 `grep`/`glob` 搜尋、切片檢視 `.log` 與 `transcript.jsonl`，以及單一檔案結構摘要。

---

## 支援的 Primary Orchestrator 前沿模型清單

Mitosis Orchestrator 全面支援各大主流 AI 廠商的前沿推理模型（自動保留您在 Antigravity UI / CLI 選擇的模型與思考等級），並透過邊界正規表示式精準攔截各家輕量級模型擔任主代理：

| 模型廠商 | 支援作為主代理的前沿模型 (Primary Orchestrator) | 自動攔截排除之輕量級模型 (Small-Tier Filter) |
| :--- | :--- | :--- |
| **xAI** | `Grok 3`、`Grok 4`、`grok-3`、`grok-4`、`grok-3-thinking` | `grok-3-mini`、`grok-3-mini-fast` |
| **Anthropic** | `Claude Opus`、`Claude Sonnet`（如 `Claude 3.7 Sonnet`、`Claude 4 Opus`） | `Claude Haiku`（`claude-3-5-haiku` 等） |
| **Google Gemini** | `Gemini 4 Argon (High / Medium)`、`Gemini Pro`、`Gemini Ultra` | `gemini-3-flash`、`gemini-flash-lite`、`gemini-nano` |
| **OpenAI** | `GPT-4`（`gpt-4o`、`gpt-4.1`、`gpt-4.5`）、`GPT-5`、`o1`、`o3`、`o4` | `gpt-4o-mini`、`gpt-4.1-mini`、`gpt-4.1-nano`、`o1-mini`、`o3-mini`、`o4-mini` |
| **DeepSeek** | `DeepSeek-R1`、`DeepSeek-V3`、`deepseek-reasoner`、`deepseek-chat` | `deepseek-lite` |
| **Alibaba Qwen** | `Qwen Max`、`Qwen Plus`、`Qwen 2.5 / 3`、`QwQ`（`qwq-32b`） | `qwen-lite`、`qwen-vl-lite` |

> [!NOTE]
> 內建非英數邊界過濾器（`(?<![a-z0-9])(flash|lite|mini|nano|haiku)(?![a-z0-9])`），無論模型名稱使用連字號 `-`、底線 `_` 或空白分隔，皆能精準攔截 `mini`、`flash`、`lite`、`haiku`、`nano` 輕量模型，同時確保 `gemini` 絕不誤判。

---

## 有絲分裂情境路由矩陣 (Mitotic Context Routing Matrix)

| 層級 | 綁定模型 | 對應組件 | 角色定位與職責範圍 |
| :--- | :--- | :--- | :--- |
| **主核架構師 (Primary Orchestrator)** | 前沿模型 (`Grok`、`Claude Opus/Sonnet`、`Gemini Argon/Pro/Ultra`、`GPT-4/5`、`o1/o3/o4`、`DeepSeek`、`Qwen`) | [`rules/AGENTS.md`](./rules/AGENTS.md) | 系統架構設計、多步驟任務拆解、跨模組重構、複雜邏輯與資安把關、向使用者釐清需求（`ask_question`）與最終彙整。 |
| **一級執行子代理 (Tier 1 Worker)** | `flash` | [`flash-worker`](./agents/flash-worker/agent.md) | 跨檔案程式碼探索、呼叫圖追蹤、外部文件與網頁查證、撰寫與執行單元/整合測試、允許清單內的程式碼修改與除錯。 |
| **二級偵察子代理 (Tier 2 Scout)** | `flash_lite` | [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) | 快速檔案與符號查找、`grep`/`glob` 搜尋、`git status`/`git log` 檢查、Log 片段擷取、單一檔案檢視與輕量摘要（無網路、無寫入）。 |

---

## 核心組件與安全防護機制

### 1. 生命週期強制守衛 ([`hooks.json`](./hooks.json) 與 [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py))
完全零外部依賴（僅使用 Python 3.8+ 標準函式庫），於每個代理生命週期自動執行以下把關：

| Hook 事件 | 防護機制與行為說明 |
| :--- | :--- |
| `SessionStart` | 讀取 `~/.gemini/antigravity-cli/settings.json`，若主模型非支援的前沿模型則發出**警告**並注入 Mitosis 編排政策；僅在啟用 `AGY_MITOSIS_PIN_SETTINGS=1` 時自動改寫設定檔。 |
| `PreInvocation` | 每回合即時檢查 `payload.modelName`；若使用者於 UI 切換至非前沿模型，每回合注入一次提醒。同時自動重設單回合編輯計數（`turn_edited_files`）與連續失敗計數（`consecutive_failures`）。 |
| `PreToolUse` · `invoke_subagent` | 依據 `TypeName`、`Workspace`、`Role` 與 `Prompt` 自動判定子代理應綁定 `flash` 或 `flash_lite`。強訊號（測試、除錯、追蹤、修改動詞）一律優先於查找關鍵字；若 `flash-lite-scout` 被指派修改任務，會自動升級為 `flash-worker`。 |
| `PreToolUse` · `send_message` | 攔截對 `flash_lite` 偵察子代理發送程式碼修改或超過 3 個編號步驟的複雜追加指令。 |
| `PreToolUse` · `run_workflow` | 攔截 `agent(...)` 呼叫未明確指定 `model="flash"` 或 `model="flash_lite"` 的工作流腳本。 |
| `PreToolUse` · `define_subagent` | 強制將 `enable_subagent_tools` 設為 `false`，防止子代理無限巢狀衍生子代理。 |
| `PreToolUse` · **情境整潔守衛 (Context Hygiene)** | 攔截主代理直接讀取 `.log` 或 `transcript.jsonl` 原始大檔，強制委派 `flash-lite-scout` 進行切片摘要，保護主代理上下文視窗不被巨量日誌污染。 |
| `PreToolUse` · **快捷修改預算 (Fast-Path Budget)** | 限制主代理單回合直接修改工作區檔案上限為 `3` 個（可由 `AGY_MITOSIS_MAX_DIRECT_EDITS` 調整），並禁止主代理在同回合直接修改檔案後執行測試框架，強制將「修改—測試」迴圈交由 `flash-worker` 執行。 |
| `PreToolUse` · **兩次失敗熔斷器 (Circuit Breaker)** | 當 `run_command` 或 `invoke_subagent` 連續失敗達 `2` 次時立即觸發熔斷，阻止無效重試迴圈並要求回報阻礙原因。 |
| `PreToolUse` · **工作區範圍守衛 (Workspace Guard)** | 選用功能（`AGY_MITOSIS_AUTO_ALLOW=1`）：工作區與 Artifact 目錄內的檔案讀寫及安全命令自動放行（`allow`）；跨出工作區、提權（`sudo`）、強制推送（`git push -f`）或危險指令則轉為詢問（`ask`）。未啟用時一律回傳明確的 `allow` 通行。 |
| `PostToolUse` | 自動將新建子代理與 `.system_generated/subagents/<cid>.json` 中繼資料（或對話紀錄）配對，並更新連續失敗計數。 |
| `Stop` | 當回合進入完全閒置且仍有存活的 `Workspace: "branch"` 分支子代理時，針對同一組分支子代理提醒一次，要求先合併或捨棄變更並呼叫 `manage_subagents(Action="kill")` 清理。 |

### 2. 有界四標頭子代理回報合約 (Bounded 4-Header Report Contract)
[`flash-lite-scout`](./agents/flash-lite-scout/agent.md) 與 [`flash-worker`](./agents/flash-worker/agent.md) 皆強制採用極簡四標頭格式回報（總長 $\le 1,600$ 字元、`NOTES:` 下最多 10 行清單條目，且當狀態為 `BLOCKED` 或 `FAILED` 時必須附上 `file://.../*.log` 錯誤日誌連結），大幅降低主核讀取回報的 Token 開銷：

```markdown
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <以逗號分隔的檔案路徑或 NONE>
VERIFICATION: <一句話說明執行的驗證命令與結果>
NOTES: <精簡摘要（可含最多 10 行 `- ` 清單）；若 BLOCKED/FAILED 須含 file:// log URI>
```

### 3. `/orchestrate` 四階段編排技能 ([`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md))
提供結構化的 `/orchestrate` 四階段工作流：
1. **階段一：平行偵察 (`flash_lite` Fan-Out)** — 同時派發多個唯讀 `flash-lite-scout` 進行高速檔案與符號探索。
2. **階段二：架構設計與拆解 (Primary Frontier Orchestrator)** — 由前沿主模型彙整偵察結果、制定架構介面並切分獨立工作單元。
3. **階段三：範圍執行與測試 (`flash` Workers)** — 派發 `flash-worker` 於指定檔案清單內完成實作與單元測試驗證。
4. **階段四：統合驗證與分支清理 (Primary Frontier Orchestrator)** — 檢視差異、完成最終驗證，並透過 `manage_subagents(Action="kill")` 清理所有分支工作樹。

---

## 環境變數設定 (`AGY_MITOSIS_*`)

全面採用 `AGY_MITOSIS_*` 環境變數，並完整向下相容舊版 `AGY_ARGON_*` 變數：

| 環境變數 | 預設值 | 功能說明 |
| :--- | :--- | :--- |
| `AGY_MITOSIS_PIN_SETTINGS` | `0` | 設為 `1` 時，於 Session 啟動時自動將 `~/.gemini/antigravity-cli/settings.json` 改寫為指定的前沿主模型。 |
| `AGY_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` | 當啟用自動綁定時的目標前沿模型名稱（例如 `Claude 3.7 Sonnet`、`grok-4`、`o3`、`qwen-max`）。 |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` | `0` | 設為 `1`（或 `AGY_MITOSIS_ALLOW_NON_PRIMARY=1`）可關閉所有非前沿主模型的警告訊息。 |
| `AGY_MITOSIS_AUTO_ALLOW` | `0` | 設為 `1` 時啟用互動模式下的工作區範圍自動放行守衛。 |
| `AGY_MITOSIS_MAX_DIRECT_EDITS` | `3` | 主代理單一回合可直接修改的不同工作區檔案數量上限。 |
| `AGY_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | 設為 `1` 時允許主代理直接讀取 `.log` 與 `transcript.jsonl` 原始檔。 |
| `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | 設為 `1` 時允許主代理繞過單回合直接修改數量限制與「修改後跑測試」限制。 |
| `AGY_MITOSIS_CIRCUIT_BREAKER` | `1` | 設為 `0` 時停用連續 2 次失敗自動熔斷機制。 |
| `AGY_MITOSIS_DEBUG` | `0` | 設為 `1` 時於 `<artifactDirectoryPath>/mitosis_orchestrator_debug.log` 記錄每次 Hook 呼叫的診斷 JSON。 |

---

## 安裝方式

### 方式一：直接 Clone 至 Antigravity 全域外掛目錄

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
```

### 方式二：透過 `agy plugin` CLI 驗證並安裝

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin validate ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
```

安裝完成後請重新啟動 `agy`。您可隨時透過 `/plugin` 介面或以下指令啟用／停用外掛：

```bash
agy plugin enable mitosis-orchestrator
agy plugin disable mitosis-orchestrator
```

---

## 開發與測試

執行完整 73 項單元測試與插件嚴格結構驗證：

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```

詳細版本更新紀錄請參閱 [CHANGELOG.md](./CHANGELOG.md)。

---

## 授權條款

[MIT](./LICENSE) © 2026 ivanusto
