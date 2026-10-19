# Mitosis Orchestrator (`mitosis-orchestrator`)

<p align="center">
  <img src="./assets/logo.svg" width="108" height="108" alt="Mitosis Orchestrator Logo" />
</p>

<p align="center">
  <strong>專為 Google Antigravity (<code>agy</code>) 與 Anthropic Claude Code 打造的次世代跨模型有絲分裂編排外掛</strong><br/>
  <a href="./README.md">English Documentation (README.md)</a> · <a href="./CHANGELOG.md">版本紀錄 (Changelog)</a> · <a href="./rules/AGENTS.md">編排規則 (Rules)</a>
</p>

---

## 專案簡介 — 為什麼命名為 Mitosis（有絲分裂）？

靈感源自生物學的**細胞有絲分裂（Mitosis）**，**Mitosis Orchestrator** 將您選用的**頂級前沿推理模型（Frontier Model）**定位為系統的**主核架構師（Master Nucleus）**。主核不再將昂貴的 Token 與上下文視窗消耗在單檔搜尋、原始 Log 翻找或反覆的「修改—跑測試」迴圈，而是將複雜的軟體工程任務有紀律地「分裂」並平行委派給兩類專職子細胞（Subagents）：

- **主核架構師（Primary Frontier Orchestrator）**：專注於高階架構設計、API 介面契約、跨模組重構決策、資安與並行性把關，以及最終結果的統合驗證。
- **一級執行子代理（`flash-worker` · `flash` on agy / `sonnet` on Claude Code）**：負責跨檔案程式碼探索、呼叫圖（Call-Graph）追蹤、網頁與技術文件查證、撰寫與執行測試，以及在明確允許清單（Allowlist）內的局部程式碼修改。
- **二級偵察子代理（`flash-lite-scout` · `flash_lite` on agy / `haiku` on Claude Code）**：負責高併發、低延遲的唯讀單跳偵察——快速定位檔案與符號、執行 `grep`/`glob` 搜尋、切片檢視 `.log` 與 transcript，以及單一檔案結構摘要。

---

## 支援的 Primary Orchestrator 前沿模型清單

Mitosis Orchestrator 全面支援各大主流 AI 廠商的前沿推理模型（自動保留您在 Antigravity 或 Claude Code 選擇的模型與思考等級），並透過邊界正規表示式精準攔截各家輕量級模型擔任主代理：

| 模型廠商 | 支援作為主代理的前沿模型 (Primary Orchestrator) | 自動攔截排除之輕量級模型 (Small-Tier Filter) |
| :--- | :--- | :--- |
| **Anthropic** | `Claude Opus`、`Claude Sonnet`（如 `Claude 3.7 Sonnet`、`Claude 4 Opus`） | `Claude Haiku`（`claude-3-5-haiku` 等） |
| **Google Gemini** | `Gemini 4 Argon (High / Medium)`、`Gemini Pro`、`Gemini Ultra` | `gemini-3-flash`、`gemini-flash-lite`、`gemini-nano` |
| **xAI** | `Grok 3`、`Grok 4`、`grok-3`、`grok-4`、`grok-3-thinking` | `grok-3-mini`、`grok-3-mini-fast` |
| **OpenAI** | `GPT-4`（`gpt-4o`、`gpt-4.1`、`gpt-4.5`）、`GPT-5`、`o1`、`o3`、`o4` | `gpt-4o-mini`、`gpt-4.1-mini`、`gpt-4.1-nano`、`o1-mini`、`o3-mini`、`o4-mini` |
| **DeepSeek** | `DeepSeek-R1`、`DeepSeek-V3`、`deepseek-reasoner`、`deepseek-chat` | `deepseek-lite` |
| **Alibaba Qwen** | `Qwen Max`、`Qwen Plus`、`Qwen 2.5 / 3`、`QwQ`（`qwq-32b`） | `qwen-lite`、`qwen-vl-lite` |

> [!NOTE]
> 內建非英數邊界過濾器（`(?<![a-z0-9])(flash|lite|mini|nano|haiku)(?![a-z0-9])`），無論模型名稱使用連字號 `-`、底線 `_` 或空白分隔，皆能精準攔截 `mini`、`flash`、`lite`、`haiku`、`nano` 輕量模型，同時確保 `gemini` 絕不誤判。

---

## 有絲分裂情境路由矩陣 (Mitotic Context Routing Matrix)

| 層級 | agy 模型 | Claude Code 模型 | 對應組件 | 角色定位與職責範圍 |
| :--- | :--- | :--- | :--- | :--- |
| **主核架構師 (Primary Orchestrator)** | 前沿模型 (`Argon`、`Opus/Sonnet`、`GPT-4/5`、`o1/o3/o4`、`Grok`、`DeepSeek`、`Qwen`) | 前沿模型 (`Claude Opus`、`Claude Sonnet 3.7`、`o1/o3`) | [`rules/AGENTS.md`](./rules/AGENTS.md) | 系統架構設計、多步驟任務拆解、跨模組重構、複雜邏輯與資安把關、向使用者釐清需求與最終彙整。 |
| **一級執行子代理 (Tier 1 Worker)** | `flash` | `sonnet` | [`agents/flash-worker`](./agents/flash-worker/agent.md)<br/>[`claude-agents/flash-worker.md`](./claude-agents/flash-worker.md) | 跨檔案程式碼探索、呼叫圖追蹤、外部文件與網頁查證、撰寫與執行單元/整合測試、允許清單內的程式碼修改與除錯。 |
| **二級偵察子代理 (Tier 2 Scout)** | `flash_lite` | `haiku` | [`agents/flash-lite-scout`](./agents/flash-lite-scout/agent.md)<br/>[`claude-agents/flash-lite-scout.md`](./claude-agents/flash-lite-scout.md) | 快速檔案與符號查找、`grep`/`glob` 搜尋、`git status`/`git log` 檢查、Log 片段擷取、單一檔案檢視與輕量摘要（無網路、無寫入）。 |

---

## 雙平台架構支援 (`agy` + Claude Code)

Mitosis Orchestrator v1.1.0 原生以單一套件同時支援 Google Antigravity 與 Anthropic Claude Code：

```text
mitosis-orchestrator/
├── plugin.json                 # Antigravity 原生 Manifest
├── .claude-plugin/
│   └── plugin.json             # Claude Code 擴充套件 Manifest
├── hooks.json                  # Antigravity 生命週期鉤子
├── hooks/
│   └── hooks.json              # Claude Code 生命週期鉤子 (${CLAUDE_PLUGIN_ROOT})
├── agents/                     # Antigravity 子代理設定 (flash / flash_lite)
│   ├── flash-worker/agent.md
│   └── flash-lite-scout/agent.md
├── claude-agents/              # Claude Code 子代理設定 (sonnet / haiku)
│   ├── flash-worker.md
│   └── flash-lite-scout.md
├── rules/
│   └── AGENTS.md               # 雙平台統一編排與路由守則
├── skills/
│   └── orchestrate/SKILL.md    # 可攜式 /orchestrate 4 階段工作流
└── scripts/
    └── enforce-orchestrator.py # 零外部依賴 Python 守衛腳本引擎
```

### 生命週期鉤子對照表

| 生命週期事件 | Antigravity (`agy`) | Claude Code | 引擎防護機制 |
| :--- | :--- | :--- | :--- |
| **工作階段初始** | `SessionStart` | `SessionStart` | 檢查主模型是否符合前沿模型清單；注入 Mitosis 路由架構政策。 |
| **回合 / 提示詞送出** | `PreInvocation` | `UserPromptSubmit` | 檢查即時回合模型；重設單回合編輯計數與連續失敗計數。 |
| **工具執行前攔截** | `PreToolUse` (`*`) | `PreToolUse` (`*`) | 分類子代理呼叫為 scout 或 worker；阻斷偵察代理的寫入行為；熔斷器；編輯預算。 |
| **工具執行後清理** | `PostToolUse` | `PostToolUse` | 記錄子代理完成資訊；追蹤連續失敗計數。 |
| **子代理結束** | 主迴圈統合處理 | `SubagentStop` | 驗收子代理產出成果後再交回控制權。 |
| **回合結束** | `Stop` | `Stop` | 檢查是否有未清理的隔離分支 / Worktree 子代理，若有則主動提醒清理。 |

---

## 環境變數與設定選項

所有設定皆支援 Antigravity 的 `AGY_MITOSIS_*` 與 Claude Code 的 `CLAUDE_MITOSIS_*`（並向下相容舊版 `AGY_ARGON_*`）：

| Antigravity 變數 | Claude Code 變數 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- |
| `AGY_MITOSIS_PIN_SETTINGS` | — | `0` | 設為 `1` 時於工作階段開始自動將 `settings.json` 改寫為目標前沿模型。 |
| `AGY_MITOSIS_PRIMARY_MODEL` | `CLAUDE_MITOSIS_PRIMARY_MODEL` | `Gemini 4 Argon (High)` | 自動鎖定之目標前沿模型名稱。 |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY` | `CLAUDE_MITOSIS_ALLOW_NON_PRIMARY` | `0` | 設為 `1` 靜音所有非前沿主模型之提示警告。 |
| `AGY_MITOSIS_AUTO_ALLOW` | `CLAUDE_MITOSIS_AUTO_ALLOW` | `0` | 設為 `1` 啟用工作區內檔案與指令工具之自動放行守衛。 |
| `AGY_MITOSIS_MAX_DIRECT_EDITS` | `CLAUDE_MITOSIS_MAX_DIRECT_EDITS` | `3` | 主代理每回合允許直接修改之工作區相異檔案上限。 |
| `AGY_MITOSIS_ALLOW_DIRECT_LOGS` | `CLAUDE_MITOSIS_ALLOW_DIRECT_LOGS` | `0` | 設為 `1` 允許主代理直接讀取 `.log` 與 transcript 檔案。 |
| `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH` | `CLAUDE_MITOSIS_ALLOW_DIRECT_FASTPATH` | `0` | 設為 `1` 繞過直接修改上限與直接「改檔—跑測試」防護。 |
| `AGY_MITOSIS_CIRCUIT_BREAKER` | `CLAUDE_MITOSIS_CIRCUIT_BREAKER` | `1` | 設為 `0` 關閉連續 2 次失敗熔斷保護機制。 |
| `AGY_MITOSIS_DEBUG` | `CLAUDE_MITOSIS_DEBUG` | `0` | 設為 `1` 將診斷日誌附加輸出至 `mitosis_orchestrator_debug.log`。 |

---

## 安裝指南

### A. Google Antigravity (`agy`)

#### 方法一：直接 Clone 至全域外掛目錄
```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
```

#### 方法二：透過 `agy plugin` CLI 安裝
```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin validate ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
```

安裝完成後重新啟動 `agy`。隨時可透過 `/plugin` 指令或命令列切換啟用狀態：
```bash
agy plugin enable mitosis-orchestrator
agy plugin disable mitosis-orchestrator
```

### B. Anthropic Claude Code

#### 方法一：使用 CLI 參數載入
```bash
claude --plugin-dir ~/.gemini/config/plugins/mitosis-orchestrator
```

#### 方法二：安裝至本機外掛目錄
```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.claude/plugins/mitosis-orchestrator
```
在 Claude Code 中執行 `/reload-plugins` 即可生效。

---

## 開發與驗證

執行完整單元測試套件並嚴格驗證外掛 Manifest：

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```

詳細版本歷程請參閱 [CHANGELOG.md](./CHANGELOG.md)。

---

## 授權條款

[MIT](./LICENSE) © 2026 ivanusto
