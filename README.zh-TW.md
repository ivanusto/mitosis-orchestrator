# Mitosis Orchestrator (`mitosis-orchestrator`)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>跨模型有絲分裂編排外掛 — 支援三大 AI CLI 平台</strong><br/>
  <em>Google Antigravity (<code>agy</code>) · Anthropic Claude Code · OpenAI Codex CLI</em><br/>
  <a href="./README.md">English Documentation (README.md)</a> · <a href="./CHANGELOG.md">版本紀錄 (Changelog)</a> · <a href="./rules/AGENTS.md">編排規則 (Rules)</a>
</p>

---

## 專案簡介 — 為什麼命名為 Mitosis（有絲分裂）？

靈感源自生物學的**細胞有絲分裂（Mitosis）**。**Mitosis Orchestrator** 將您選用的**尖端推理模型（Frontier Model）**定位為系統的**主核架構師（Master Nucleus）**。主核不再把昂貴的 Token 與上下文視窗消耗在單檔搜尋、原始 Log 翻找或反覆的「修改—跑測試」迴圈，而是將複雜的工程任務有紀律地「分裂」並平行委派給兩類專職子細胞（Subagents）：

- **主核架構師（Primary Frontier Orchestrator）**：專注於高階架構設計、API 介面契約、跨模組重構決策、資安與並行性把關，以及最終結果的統合驗證。
- **執行子代理（`flash-worker` · `worker`）**：負責跨檔案程式碼探索、呼叫圖（Call-Graph）追蹤、外部文件查證、撰寫與執行測試，以及在明確允許清單內的局部修改。
- **偵察子代理（`flash-lite-scout` · `explorer`）**：負責高併發、低延遲的唯讀單跳偵察：快速定位檔案與符號、執行 `grep`/`glob` 搜尋、切片檢視 `.log` 與 `transcript.jsonl`，以及單一檔案結構摘要。

---

## 支援的主核尖端模型清單

Mitosis Orchestrator 在三大平台都支援尖端推理模型擔任主核，並透過邊界正規表示式攔截輕量模型擔任主代理：

| 模型廠商 | 支援作為主代理的尖端模型 (Primary Orchestrator) | 自動攔截的輕量級模型 (Small-Tier Filter) |
| :--- | :--- | :--- |
| **OpenAI** (Codex CLI / API) | **`gpt-6.1-sol`**、**`gpt-5.6-sol`**、`gpt-5.6-terra`、`gpt-6-astra`、`o1`、`o3`、`o4` | `gpt-5.6-luna`、`gpt-5.4-mini`、`o4-mini`、`o3-mini`、`gpt-4.1-mini` |
| **Anthropic** (Claude Code) | **`fable`**（Claude Fable 5.1）、**`opus`**（Claude Opus 5.5）、`sonnet`（Claude Sonnet 5.5） | `haiku`（Claude Haiku 5.5、`claude-haiku-*`） |
| **Google Gemini** (Antigravity `agy`) | `Gemini 4 Argon (High / Medium)`、`Gemini Pro`、`Gemini Ultra` | `gemini-3-flash`、`gemini-flash-lite`、`gemini-nano` |
| **xAI** | `Grok 3`、`Grok 4`、`grok-3`、`grok-4`、`grok-3-thinking` | `grok-3-mini`、`grok-3-mini-fast` |
| **DeepSeek** | `DeepSeek-R1`、`DeepSeek-V3`、`deepseek-reasoner`、`deepseek-chat` | `deepseek-lite` |
| **Alibaba Qwen** | `Qwen Max`、`Qwen Plus`、`Qwen 2.5 / 3`、`QwQ`（`qwq-32b`） | `qwen-lite`、`qwen-vl-lite` |

> [!NOTE]
> 內建邊界過濾器（`(?<![a-z0-9])(flash|lite|mini|nano|haiku|luna|spark)(?![a-z0-9])`）排除 `mini`、`flash`、`lite`、`haiku`、`nano`、`luna`、`spark` 輕量模型，同時確保 `gemini` 不會誤判。

---

## 三平台情境路由矩陣 (v1.3.1)

| 層級 | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI | 角色定位與職責範圍 |
| :--- | :--- | :--- | :--- | :--- |
| **主核架構師 (Master Nucleus)** | `Gemini 4 Argon (High/Medium)` / 尖端模型 | `fable` / `opus` / `sonnet` | **`gpt-6.1-sol`** / `gpt-5.6-sol` / `gpt-6-astra` / `o3` | 系統架構設計、多步驟任務拆解、跨模組重構、複雜邏輯與資安把關、向使用者釐清需求與最終彙整。 |
| **執行子代理 (Worker)** | `flash` via [`flash-worker`](./agents/flash-worker/agent.md) | `sonnet` via [`mitosis-orchestrator:flash-worker`](./claude-agents/flash-worker.md) | **`gpt-5.6-terra`** via [`worker`](./codex-agents/worker.toml) / [`flash-worker`](./codex-agents/flash-worker.toml) 或 `--profile mitosis-worker` | 跨檔案探索、呼叫圖追蹤、撰寫與執行單元/整合測試、允許清單內的局部修改（`workspace-write`）。 |
| **偵察子代理 (Scout)** | `flash_lite` via [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) | `haiku` via [`mitosis-orchestrator:flash-lite-scout`](./claude-agents/flash-lite-scout.md) | **`gpt-5.6-luna`** via [`explorer`](./codex-agents/explorer.toml) / [`flash-lite-scout`](./codex-agents/flash-lite-scout.toml) 或 `--profile mitosis-scout` | 快速檔案與符號查找、`grep`/`glob` 搜尋、`git status`/`git log` 檢查、Log 切片檢視與輕量摘要（`read-only`）。 |

---

## 安裝與設定方式

需求：Python 3.11 以上（只用標準函式庫；Codex 安裝腳本用到 `tomllib`）、`git`。

### 平台一：Google Antigravity (`agy` 1.4 以上)

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
agy plugin list            # mitosis-orchestrator (enabled, 1.3.0)
```

也可以直接 clone 到全域外掛目錄，`agy` 會自動載入該目錄下的每個外掛：

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
agy plugin validate ~/.gemini/config/plugins/mitosis-orchestrator --strict
```

> [!WARNING]
> 若仍裝著前身 `argon-orchestrator`，請先移除（`agy plugin uninstall argon-orchestrator`）。兩者的 skill 都叫 `orchestrate`、agent 都叫 `flash-worker` / `flash-lite-scout`，同時啟用會互相衝突並跑兩套 hook。

### 平台二：Anthropic Claude Code (2.1.292 以上)

本 repo 本身就是單一外掛的 marketplace（`.claude-plugin/marketplace.json`），可直接從 GitHub 安裝：

```bash
claude plugin marketplace add ivanusto/mitosis-orchestrator
claude plugin install mitosis-orchestrator@mitosis-orchestrator
claude plugin list         # mitosis-orchestrator@mitosis-orchestrator  ✔ enabled
```

之後更新：`claude plugin marketplace update mitosis-orchestrator && claude plugin update mitosis-orchestrator@mitosis-orchestrator`。

本機開發時改用本機路徑當 marketplace（`claude plugin marketplace add /path/to/mitosis-orchestrator`），或只在單次 session 載入：

```bash
claude --plugin-dir /path/to/mitosis-orchestrator
```

載入內容：`hooks/hooks.json`（SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / SubagentStop / Stop）、`/orchestrate` skill，以及兩個可由 `Agent` 工具呼叫的子代理：`subagent_type: "mitosis-orchestrator:flash-lite-scout"`（haiku）與 `subagent_type: "mitosis-orchestrator:flash-worker"`（sonnet）。驗證方式：開新 session 後應看到 `[Mitosis Orchestrator Active] Primary model is 'fable'.` 橫幅，且 `/orchestrate` 出現在指令清單。

### 平台三：OpenAI Codex CLI (0.134 以上；已於 0.154.0 與 0.163.0 驗證)

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
python3 ~/mitosis-orchestrator/scripts/setup-codex.py
codex exec --profile mitosis-scout "Locate where parse_token is defined"
```

安裝腳本可重複執行，依序完成：

1. **自動備份**：將 `$CODEX_HOME/config.toml` 複製為 `config.toml.bak`（`$CODEX_HOME` 預設 `~/.codex`）。
2. **同步規則**：將 `rules/AGENTS.md` 複製至 `$CODEX_HOME/AGENTS.md`。
3. **安裝角色**：將角色檔安裝至 `$CODEX_HOME/agents/`：`explorer.toml` 與 `flash-lite-scout.toml`（`gpt-5.6-luna`、`low`）、`worker.toml` 與 `flash-worker.toml`（`gpt-5.6-terra`、`medium`）。主代理可透過 `spawn_agent(agent_type=...)` 選用。
4. **安裝 profile**：將 `mitosis-scout.config.toml` 與 `mitosis-worker.config.toml` 複製至 `$CODEX_HOME/`（Codex 0.134 起 `--profile <name>` 讀取 `$CODEX_HOME/<name>.config.toml`，舊的 `[profiles.*]` 區段已不再生效）。
5. **注入 MCP**：在 `config.toml` 寫入 `[mcp_servers.mitosis]`，使用執行安裝腳本的 Python 直譯器與 `scripts/mitosis-mcp-server.py` 的絕對路徑。
6. **完整性驗證**：校驗所有 TOML 並確認檔案存在。

MCP server 執行偵察子代理時使用 `--sandbox read-only`，執行子代理使用 `--sandbox workspace-write`；只有設定 `MITOSIS_CODEX_YOLO=1` 才會改用 `--dangerously-bypass-approvals-and-sandbox`。

---

## 核心組件與安全防護機制

### 1. 生命週期強制守衛 ([`hooks.json`](./hooks.json) · [`hooks/hooks.json`](./hooks/hooks.json) · [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py))
零外部依賴（僅 Python 3.8+ 標準函式庫），每次呼叫約 30 毫秒，於每個代理生命週期自動把關：

| Hook 事件 | 防護機制與行為說明 |
| :--- | :--- |
| `SessionStart` | 讀取設定檔，若主模型不是支援的尖端模型則發出**警告**並注入 Mitosis 編排政策；僅在啟用 `*_MITOSIS_PIN_SETTINGS=1` 時改寫設定檔。 |
| `PreInvocation`（agy）· `UserPromptSubmit`（Claude Code / Codex） | 每回合檢查即時模型；若切換到非尖端模型，每回合提醒一次。同時重設單回合編輯計數（`turn_edited_files`）與連續失敗計數（`consecutive_failures`）。 |
| `PreToolUse` · `invoke_subagent`（agy） | 自動判定子代理應綁定執行或偵察層級。強訊號（測試、除錯、修改動詞）一律優先於查找關鍵字；偵察子代理被指派修改任務時自動升級為執行子代理。 |
| `PreToolUse` · `Agent`（Claude Code） | **只**改寫本外掛自己的型別（`flash-worker`、`flash-lite-scout`，有無 `mitosis-orchestrator:` 前綴皆可）與未指定型別且 `model: inherit` 的 `Agent` 呼叫。其他外掛的 agent、內建型別（`Explore`、`Plan`、`general-purpose` 等）與明確指定模型的呼叫一律原樣放行。`CLAUDE_MITOSIS_PIN_SUBAGENTS=0` 可整個關閉。 |
| `PreToolUse` · `spawn_agent` / `codex exec`（Codex） | 依提示詞分類，將子代理綁定到 `gpt-5.6-luna`（偵察）或 `gpt-5.6-terra`（執行）。 |
| `PreToolUse` · `send_message` | 攔截對偵察子代理發送程式碼修改或超過 3 個編號步驟的追加指令。 |
| `PreToolUse` · `run_workflow` | 攔截工作流腳本中未明確指定子代理模型層級的呼叫。 |
| `PreToolUse` · `define_subagent` | 強制 `enable_subagent_tools=false`，防止子代理無限巢狀衍生。 |
| `PreToolUse` · **情境整潔守衛 (Context Hygiene)** | 攔截主代理直接讀取 `.log` 或 `transcript.jsonl` 原始大檔，改由偵察子代理切片摘要。 |
| `PreToolUse` · **快捷修改預算 (Fast-Path Budget)** | 限制主代理單回合直接修改工作區檔案上限 `3` 個（`*_MITOSIS_MAX_DIRECT_EDITS` 可調），並禁止主代理在同回合直接修改檔案後執行測試框架。 |
| `PreToolUse` · **兩次失敗熔斷器 (Circuit Breaker)** | 指令或子代理呼叫連續失敗達 `2` 次即熔斷，阻止無效重試並要求回報阻礙原因。 |
| `PreToolUse` · **工作區範圍守衛 (Workspace Guard)** | 選用（`*_MITOSIS_AUTO_ALLOW=1`）：工作區內的檔案讀寫與安全命令自動放行；跨出工作區、提權或危險指令轉為詢問（`ask`；Codex 沒有 `ask`，改為 `deny`）。未啟用時一律回傳明確的 `allow`。 |
| `PostToolUse` | 配對新建子代理中繼資料，並更新連續失敗計數。 |
| `SubagentStop` / `Stop` | 回合閒置且仍有存活的分支工作樹時，提醒主代理先合併或捨棄變更並清理。 |

### 2. 有界四標頭子代理回報合約 (Bounded 4-Header Report Contract)
所有偵察與執行子代理都採用極簡四標頭格式回報（總長 ≤ 1,600 字元、`NOTES:` 下最多 10 行清單，狀態為 `BLOCKED` 或 `FAILED` 時必須附 `file://.../*.log` 日誌連結）：

```markdown
STATUS: COMPLETED | BLOCKED | FAILED
FILES_CHANGED: <以逗號分隔的檔案路徑或 NONE>
VERIFICATION: <一句話說明執行的驗證命令與結果>
NOTES: <精簡摘要（可含最多 10 行 `- ` 清單）；若 BLOCKED/FAILED 須含 file:// log URI>
```

### 3. `/orchestrate` 四階段編排技能 ([`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md))
1. **階段一：平行偵察 (Scout / Explorer Fan-Out)**：派發唯讀偵察子代理進行高速檔案與符號探索。
2. **階段二：架構設計與拆解 (Primary Master Nucleus)**：由主核彙整偵察結果、制定架構介面並切分獨立工作單元。
3. **階段三：範圍執行與測試 (Workers)**：派發執行子代理於指定檔案清單內完成實作與測試驗證。
4. **階段四：統合驗證與分支清理 (Primary Master Nucleus)**：檢視差異、完成最終驗證並清理工作樹。

---

## 環境變數設定

所有變數都接受 `AGY_MITOSIS_*`、`CLAUDE_MITOSIS_*`、`CODEX_MITOSIS_*` 三種前綴（舊版 `AGY_ARGON_*` 仍相容）：

| 環境變數 | 預設值 | 功能說明 |
| :--- | :--- | :--- |
| `*_MITOSIS_PIN_SETTINGS` | `0` | 設為 `1` 時，Session 啟動時自動把設定檔改寫為指定的主核尖端模型。 |
| `*_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` · `opus` · `gpt-6.1-sol` | 啟用自動綁定時的目標模型（依平台預設）。 |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` / `*_MITOSIS_ALLOW_NON_PRIMARY` | `0` | 設為 `1` 可關閉所有非尖端主模型的警告。 |
| `CLAUDE_MITOSIS_PIN_SUBAGENTS` | `1` | 設為 `0` 時 Claude Code 端完全不改寫 `Agent` 呼叫。 |
| `*_MITOSIS_AUTO_ALLOW` | `0` | 設為 `1` 時啟用互動模式下的工作區範圍自動放行守衛。 |
| `*_MITOSIS_MAX_DIRECT_EDITS` | `3` | 主代理單一回合可直接修改的不同工作區檔案數量上限。 |
| `*_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | 設為 `1` 時允許主代理直接讀取 `.log` 與 `transcript.jsonl`。 |
| `*_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | 設為 `1` 時允許主代理繞過單回合修改數量限制與「修改後跑測試」限制。 |
| `*_MITOSIS_CIRCUIT_BREAKER` | `1` | 設為 `0` 時停用連續 2 次失敗熔斷機制。 |
| `*_MITOSIS_DEBUG` | `0` | 設為 `1` 時在狀態檔旁的 `mitosis_orchestrator_debug.log` 記錄每次 Hook 呼叫的診斷 JSON。 |
| `MITOSIS_CODEX_YOLO` | `0` | 設為 `1` 時 Codex 子代理改用 `--dangerously-bypass-approvals-and-sandbox` 而非分級沙箱。 |

---

## 開發與測試

```bash
python3 -m unittest discover -s tests -v     # 144 個測試
agy plugin validate . --strict
claude --plugin-dir . -p "/orchestrate smoke test"   # Claude Code 試跑（選用）
```

詳細版本更新紀錄請參閱 [CHANGELOG.md](./CHANGELOG.md)。

---

## 授權條款

[MIT](./LICENSE) © 2026 ivanusto
