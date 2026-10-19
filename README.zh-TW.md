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

## 三平台情境路由矩陣 (v1.3.2)

| 層級 | Google Antigravity (`agy`) | Anthropic Claude Code | OpenAI Codex CLI | 角色定位與職責範圍 |
| :--- | :--- | :--- | :--- | :--- |
| **主核架構師 (Master Nucleus)** | `Gemini 4 Argon (High/Medium)` / 尖端模型 | `fable` / `opus` / `sonnet` | **`gpt-6.1-sol`** / `gpt-5.6-sol` / `gpt-6-astra` / `o3` | 系統架構設計、多步驟任務拆解、跨模組重構、複雜邏輯與資安把關、向使用者釐清需求與最終彙整。 |
| **執行子代理 (Worker)** | `flash` via [`flash-worker`](./agents/flash-worker/agent.md) | `sonnet` via [`mitosis-orchestrator:flash-worker`](./claude-agents/flash-worker.md) | **`gpt-5.6-terra`** via [`worker`](./codex-agents/worker.toml) / [`flash-worker`](./codex-agents/flash-worker.toml) 或 `--profile mitosis-worker` | 跨檔案探索、呼叫圖追蹤、撰寫與執行單元/整合測試、允許清單內的局部修改（`workspace-write`）。 |
| **偵察子代理 (Scout)** | `flash_lite` via [`flash-lite-scout`](./agents/flash-lite-scout/agent.md) | `haiku` via [`mitosis-orchestrator:flash-lite-scout`](./claude-agents/flash-lite-scout.md) | **`gpt-5.6-luna`** via [`explorer`](./codex-agents/explorer.toml) / [`flash-lite-scout`](./codex-agents/flash-lite-scout.toml) 或 `--profile mitosis-scout` | 快速檔案與符號查找、`grep`/`glob` 搜尋、`git status`/`git log` 檢查、Log 切片檢視與輕量摘要（`read-only`）。 |

---

## Claude Code 主核模型選擇：Fable 5.1 與 Opus 5.5

Mitosis 只釘定子細胞（scout 用 `haiku`、worker 用 `sonnet`），主核（Master Nucleus）就是 `/model` 目前選的模型，在 Claude Code 上通常是 `fable`（Claude Fable 5.1）或 `opus`（Claude Opus 5.5）。下表為撰寫時（2026 年 10 月）Anthropic API 牌價，單位為美元／百萬 token；實際採用前請以官方價目頁為準。

| 階層 | 模型 | 輸入 | 輸出 | 快取讀取 | 相對 Opus 5.5 | 預設 effort |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| 主核 | `fable`（Claude Fable 5.1） | 10.00 | 50.00 | 0.25 | 2.5 倍 | `high` |
| 主核 | `opus`（Claude Opus 5.5） | 4.00 | 20.00 | 0.20 | 1 倍 | `medium` |
| Worker | `sonnet`（Claude Sonnet 5.5） | 2.00 | 10.00 | 0.20 | 0.5 倍 | `high`（Mitosis 釘為 `medium`） |
| Scout | `haiku`（Claude Haiku 5.5） | 0.10 | 0.50 | 約 0.01 | 0.025 倍 | `medium`（Mitosis 釘為 `low`） |

### 換主核會改變什麼

- **單價差 2.5 倍。** Fable 5.1 與 Opus 5.5 屬同一 tokenizer 家族，同樣的對話在 Fable 上約貴 2.5 倍。在 Mitosis 架構下這個倍率只作用在主核的部分（規劃、拆解、審查 diff、整合）；scout 與 worker 的費用在兩種主核下完全相同。
- **任務時間。** Fable 5.1 的單一回合較長：在 `high` effort 下，一個需要蒐集情境、實作並自我驗證的請求常跑數分鐘，困難任務單一回合跑到 15 分鐘屬於正常現象。Opus 5.5 預設 `medium`，在這個等級就能以更少步驟與約一半的 token 達到或超過 Opus 5 的 `high`。兩者在 `xhigh` / `max` 都會明顯變慢。effort 是兩種模型共同的時間與費用主要控制桿：session 內用 `/effort`，或設定 `~/.claude/settings.json` 的 `effortLevel`，或環境變數 `CLAUDE_CODE_EFFORT_LEVEL`。
- **能力。** Fable 5.1 在長程自主執行、需要主核自行釐清的模糊需求、牽涉隱含不變量的跨模組重構、深度程式碼審查，以及長時間協調平行子代理這些工作上領先；它的 `low` / `medium` 仍然很強，常超過前代模型的 `xhigh`。Opus 5.5 在規格明確的多步驟 agentic 開發與審查上性價比更好：不只每 token 便宜，每個解完的任務也更便宜。
- **錢花在哪裡。** 編排 session 的帳單由主核的快取讀取與輸出主導，即使所有 scout 與 worker 都已釘定也是如此（下方兩場實測 session 中主核占總費用 70% 到 85%）。未釘定的子代理會繼承主核模型：內建的 `Explore` / `general-purpose` 代理與帶明確 `model` 的未指定型別 `Agent` 呼叫，hook 刻意不動它們；實測中一個繼承 `fable` 的子代理，費用約為十三個 `haiku` scout 的 40 倍。

### 建議

| 情境 | 主核 | Effort |
| :--- | :--- | :--- |
| 日常編排、規格明確的任務（預設） | `opus` | `medium`；架構為主的回合用 `high` |
| 對成本敏感的大量例行任務 | `opus` | `low` / `medium` |
| 數小時無人看管的自主長跑 | `fable` | `medium`；只有最難的部分用 `high` |
| 需求模糊，需要主核自行釐清 | `fable` | `medium` |
| 牽涉不變量、並行或安全性的跨模組重構 | `fable` | `high` |
| 大型 diff 的深度審查或 repo 歷史追查 | `fable` | `medium` |

外掛在 Claude Code 上的預設釘定目標是 `opus`，理由相同。經驗法則：若成本報表顯示主核占了三分之二以上的 token，代表你正以主核的價格在做子細胞該做的事。先修委派（使用型別化的 `mitosis-orchestrator:*` 子代理、不要繼承模型），再考慮換模型。

**示例試算**（純假設，非實測）：主核每個任務讀 300K 輸入 token（80% 來自快取）、寫 30K 輸出 token。

| 主核 | 輸入（60K） | 快取讀取（240K） | 輸出（30K） | 每任務 |
| :--- | ---: | ---: | ---: | ---: |
| `fable` | 0.60 | 0.06 | 1.50 | 約 2.16 美元 |
| `opus` | 0.24 | 0.05 | 0.60 | 約 0.89 美元 |

光主核就差 2.4 倍，scout 與 worker 的費用兩列相同。真實 session 的回合數與 effort 都不同，請自行量測。

**訂閱方案（Pro / Max）。** 訂閱不按 token 計費，但用量上限同樣依模型階層加權，方向一致，所以 `fable` 主核消耗上限的速度明顯快於 `opus`。上述判斷原則不變；下方報表仍能告訴你是哪個模型在吃額度。

### 實際量測：`scripts/mitosis-cost-report.py`

只用標準函式庫的腳本，讀取 Claude Code transcript（`~/.claude/projects/<專案>/<session>.jsonl` 與 `<session>/subagents/agent-*.jsonl`），依訊息 id 去除串流重複的用量記錄，按模型彙總 token，區分主核與子代理，並以內建價目表估算美元（`--prices file.json` 可覆寫）。快取寫入對主核採 1 小時費率、對子代理採 5 分鐘費率（`--primary-cache-ttl` / `--subagent-cache-ttl`），在已驗證的 session 上能精確重現 Claude Code 自己的費用數字（`claude -p --output-format json`）：

```bash
python3 scripts/mitosis-cost-report.py                     # 目前專案最新的 session
python3 scripts/mitosis-cost-report.py --last 2            # 最新兩個 session 並排比較
python3 scripts/mitosis-cost-report.py --session <id-A> --session <id-B> --json
```

兩場真實（但不同）任務的節錄輸出，各用一種主核：

```text
  model                 role        turns      input    cache_w     cache_r    output  thinking       USD
  claude-fable-5-1      primary        60      1,740    252,164  10,664,346    99,292    22,559    12.691
  claude-fable-5-1      subagent       17        514    127,169   1,449,838     4,974       241     2.206
  claude-haiku-5-5      subagent       13         26    171,781   1,520,729    27,748    18,087     0.051
  TOTAL USD 14.948   primary share: 77% of tokens, 85% of USD

Comparison
  session       primary model(s)                    duration  primary USD  subagent USD  total USD
  e93f5d3f-0a4  claude-fable-5-1                    1h19m02s       12.691         2.256     14.948
  25dcc449-ba9  claude-opus-5-5                       58m29s        8.153         3.467     11.620
```

下結論前請用同一個任務分別跑兩種主核；上面兩場是不同的工作。其中 `claude-fable-5-1 subagent` 那一列就是前述的繼承模型陷阱。

### 調整子細胞

自 1.3.2 起，Claude Code 的 hook 在改寫 `Agent` 呼叫時會同時釘定 `effort` 與 `model`：scout 用 `low`、worker 用 `medium`（與 Codex 角色相同）。呼叫端若已帶合法的 `effort` 則予以尊重。可依階層覆寫或整個關閉：

```bash
export CLAUDE_MITOSIS_WORKER_MODEL=opus      # fable 主核搭配較強的 worker（haiku|sonnet|opus|fable）
export CLAUDE_MITOSIS_SCOUT_EFFORT=medium    # low|medium|high|xhigh|max
export CLAUDE_MITOSIS_PIN_EFFORT=0           # 不再加 effort；模型仍會釘定
```

---

## 安裝與設定方式

需求：Python 3.11 以上（只用標準函式庫；Codex 安裝腳本用到 `tomllib`）、`git`。

### 平台一：Google Antigravity (`agy` 1.4 以上)

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
agy plugin list            # mitosis-orchestrator (enabled, 1.3.2)
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

載入內容：`hooks/hooks.json`（SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / SubagentStop / Stop）、`/orchestrate` skill，以及兩個可由 `Agent` 工具呼叫的子代理：`subagent_type: "mitosis-orchestrator:flash-lite-scout"`（haiku，effort `low`）與 `subagent_type: "mitosis-orchestrator:flash-worker"`（sonnet，effort `medium`）。驗證方式：開新 session 後應看到 `[Mitosis Orchestrator Active] Primary model is 'fable'.` 橫幅，且 `/orchestrate` 出現在指令清單。

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
| `PreToolUse` · `Agent`（Claude Code） | **只**改寫本外掛自己的型別（`flash-worker`、`flash-lite-scout`，有無 `mitosis-orchestrator:` 前綴皆可）與未指定型別且 `model: inherit` 的 `Agent` 呼叫。其他外掛的 agent、內建型別（`Explore`、`Plan`、`general-purpose` 等）與明確指定模型的呼叫一律原樣放行。同時釘定 `effort`（scout `low`、worker `medium`；呼叫端自帶的合法值優先；`CLAUDE_MITOSIS_PIN_EFFORT=0` 關閉）。`CLAUDE_MITOSIS_PIN_SUBAGENTS=0` 可整個關閉。 |
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
| `CLAUDE_MITOSIS_SCOUT_MODEL` · `CLAUDE_MITOSIS_WORKER_MODEL` | `haiku` · `sonnet` | Claude Code 各階層釘定的模型（`haiku`、`sonnet`、`opus`、`fable`）；不合法的值會退回預設。 |
| `CLAUDE_MITOSIS_PIN_EFFORT` | `1` | 設為 `0` 時 Claude Code 端不再為釘定的 `Agent` 呼叫加上 `effort`（模型仍會釘定）。 |
| `CLAUDE_MITOSIS_SCOUT_EFFORT` · `CLAUDE_MITOSIS_WORKER_EFFORT` | `low` · `medium` | Claude Code 各階層釘定的 effort（`low`、`medium`、`high`、`xhigh`、`max`）；呼叫端自帶的合法 `effort` 優先。 |
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
python3 -m unittest discover -s tests -v     # 159 個測試（hook 與成本報表）
agy plugin validate . --strict
claude --plugin-dir . -p "/orchestrate smoke test"   # Claude Code 試跑（選用）
python3 scripts/mitosis-cost-report.py --last 1       # 最新一場 Claude Code session 的 token 與費用報表
```

詳細版本更新紀錄請參閱 [CHANGELOG.md](./CHANGELOG.md)。

---

## 授權條款

[MIT](./LICENSE) © 2026 ivanusto
