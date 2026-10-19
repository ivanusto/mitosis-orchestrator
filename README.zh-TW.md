# Mitosis Orchestrator (`mitosis-orchestrator`)

[English Documentation (README.md)](./README.md)

專為 Google Antigravity (`agy`) 設計的 Orchestrator 插件：將主代理（Primary Agent）綁定為受支援的前沿模型 — **Gemini 4 Argon (`High` 或 `Medium`)**、**Gemini Pro / Ultra**、**Claude (`Opus` / `Sonnet`)**、**xAI Grok**、**OpenAI (GPT-4 / GPT-5 / o1 / o3 / o4)**、**DeepSeek** 或 **Qwen**（自動保留您在 UI/CLI 切換的思考等級，並自動過濾排除 mini / flash / lite / haiku / nano 等小型模型），並根據任務情境（Context）自動將所有子代理（Subagents）固定為 **`flash`** 或 **`flash_lite`** 模型。

---

## 架構與情境路由策略 (Context Routing Matrix)

| 層級 | 綁定模型 | 角色與負責範圍 |
| :--- | :--- | :--- |
| **主代理 (Primary Orchestrator)** | `Gemini 4 Argon (High / Medium)`、`Gemini Pro / Ultra`、`Claude (Opus / Sonnet)`、`xAI Grok`、`OpenAI (GPT-4 / GPT-5 / o1 / o3 / o4)`、`DeepSeek` 或 `Qwen`（排除小型層級：mini / flash / lite / haiku / nano） | 系統架構設計、多步驟任務拆解、跨模組重構、複雜邏輯與資安把關、最終結果彙整與驗證（自動依您選用的思考等級運行）。 |
| **一級子代理 (`flash-worker`)** | `flash` | 跨檔案程式碼探索、外部文件與網頁查證、撰寫與執行單元/整合測試、局部範圍程式碼修改與除錯。 |
| **二級子代理 (`flash-lite-scout`)** | `flash_lite` | 快速檔案/符號查找、Grep/Glob 搜尋、Git/Log 狀態檢查、單一檔案檢視、排版檢查與輕量摘要。 |

---

## 插件內含組件

- **[`rules/AGENTS.md`](./rules/AGENTS.md)**：常駐規則：情境路由矩陣、呼叫規範、有界四欄位子代理回報合約，以及 branch worktree 清理規則。
- **[`hooks.json`](./hooks.json) 與 [`scripts/enforce-orchestrator.py`](./scripts/enforce-orchestrator.py)**（Python 3.8+，僅用標準函式庫）：

  | Hook | 作用 |
  | :--- | :--- |
  | `SessionStart` | 讀取 `~/.gemini/antigravity-cli/settings.json`，主模型若非支援的前沿模型就**警告**並注入政策提示。只有設定 `AGY_MITOSIS_PIN_SETTINGS=1`（或相容 `AGY_ARGON_PIN_SETTINGS=1`）才會改寫檔案。 |
  | `PreInvocation` | 每回合檢查實際 session 模型；若非前沿層級，每次 invocation 警告一次（可抓到 session 開始後在 UI 切換模型的情況）。重設每回合追蹤計數（`turn_edited_files`、`consecutive_failures`）。 |
  | `PreToolUse` `invoke_subagent` | 依 `TypeName`、`Workspace`、`Role`、`Prompt` 把每個子代理分到 `flash` / `flash_lite`。強訊號（測試、除錯、追蹤、修改動詞）一律優先於查找關鍵字。`flash-lite-scout` 收到修改任務會自動升級為 `flash-worker`。熔斷器觸發時予以攔截。 |
  | `PreToolUse` `send_message` | 拒絕對 `flash_lite` 收件者送出程式修改或多步驟（超過 3 條編號規則）的後續指令。 |
  | `PreToolUse` `run_workflow` | 拒絕 `agent(...)` 呼叫缺少 `model="flash"` / `model="flash_lite"` 的腳本。 |
  | `PreToolUse` `define_subagent` | 強制 `enable_subagent_tools=false`（禁止巢狀再生子代理）。 |
  | `PreToolUse` 情境整潔與快捷守衛 | 攔截主代理直接檢視 `.log` 或 `transcript.jsonl` 原始檔（指派給 `flash-lite-scout`）。限制主代理單回合直接修改工作區檔案至多 3 個，並禁止同回合內直接修改後執行測試框架（指派給 `flash-worker`）。 |
  | `PreToolUse` 其他工具與工作區守衛 | 當 `AGY_MITOSIS_AUTO_ALLOW=1`（或相容 `AGY_ARGON_AUTO_ALLOW=1`）時，工作區內檔案與命令工具回 `allow`；外側路徑、提權等回 `ask`。未啟用或無路徑參數的工具直接回傳明確 `allow` 通行，絕不因 hook 輸出空白而遭軟拒絕。 |
  | `PostToolUse` | 以中繼資料配對子代理；追蹤命令與子代理連續失敗次數以驅動熔斷器。 |
  | `Stop` | 回合完全閒置時若仍有存活的 `Workspace:"branch"` worker，針對同一組 worker 只提醒一次，要求先合併/捨棄並 kill。絕不阻擋使用者取消、錯誤或預算上限。 |

- **[`agents/`](./agents/)**：
  - **[`flash-lite-scout`](./agents/flash-lite-scout/agent.md)**：綁定 `model: flash_lite` 的唯讀偵察子代理；只有本機查找工具，無網路、無寫入。
  - **[`flash-worker`](./agents/flash-worker/agent.md)**：綁定 `model: flash` 的研究與執行子代理；只修改提示詞允許清單內的檔案，失敗兩次即停止回報。
  - 兩者都回傳有界的四標頭報告（`STATUS / FILES_CHANGED / VERIFICATION / NOTES`，支援 `NOTES` 下至多 10 行清單條目，長度 $\le 1,600$ 字元，並於 `BLOCKED` 或 `FAILED` 時強制附帶 `file://` log 連結），讓主代理讀回的 token 降到最低。
- **[`skills/orchestrate/SKILL.md`](./skills/orchestrate/SKILL.md)**：`/orchestrate` 四階段流程（`flash_lite` 偵察 → 前沿模型規劃 → `flash` 執行 → 驗證與 branch 清理）。

### 環境變數

支援所有原生 `AGY_MITOSIS_*` 環境變數，並相容舊版 `AGY_ARGON_*`：

| 變數 | 效果 |
| :--- | :--- |
| `AGY_MITOSIS_PIN_SETTINGS=1` | 選擇性啟用：session 開始時把 `settings.json` 改回受支援的前沿模型（亦相容 `AGY_ARGON_PIN_SETTINGS=1`）。 |
| `AGY_MITOSIS_PRIMARY_MODEL` | 改寫時的目標模型：`Gemini 4 Argon (High)`（預設）、`Gemini 4 Argon (Medium)`、`Claude 3.7 Sonnet` 或任何受支援的前沿模型（亦相容 `AGY_ARGON_PRIMARY_MODEL`）。 |
| `AGY_ALLOW_NON_MITOSIS_PRIMARY=1` | 關閉所有主模型警告（亦相容 `AGY_ALLOW_NON_ARGON_PRIMARY=1`）。 |
| `AGY_MITOSIS_AUTO_ALLOW=1` | 啟用工作區範圍守衛（預設關閉；亦相容 `AGY_ARGON_AUTO_ALLOW=1`）。 |
| `AGY_MITOSIS_MAX_DIRECT_EDITS=<int>` | 主代理單回合直接修改工作區檔案數量上限（預設：3；亦相容 `AGY_ARGON_MAX_DIRECT_EDITS`）。 |
| `AGY_MITOSIS_ALLOW_DIRECT_LOGS=1` | 允許主代理直接讀取原始 `.log` 與 `transcript.jsonl` 檔（亦相容 `AGY_ARGON_ALLOW_DIRECT_LOGS=1`）。 |
| `AGY_MITOSIS_ALLOW_DIRECT_FASTPATH=1` | 允許主代理繞過直接修改上限與修改後執行測試框架之限制（亦相容 `AGY_ARGON_ALLOW_DIRECT_FASTPATH=1`）。 |
| `AGY_MITOSIS_CIRCUIT_BREAKER=0` | 停用 2 次失敗熔斷機制（亦相容 `AGY_ARGON_CIRCUIT_BREAKER=0`）。 |
| `AGY_MITOSIS_DEBUG=1` | 每次 hook 呼叫在 `<artifactDirectoryPath>/mitosis_orchestrator_debug.log` 追加一行 JSON（模式、工具、payload 欄位、決策；亦相容 `AGY_ARGON_DEBUG=1`）。 |

---

## 安裝方式

### 方式一：直接 Clone 至 Antigravity 全域插件目錄

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/.gemini/config/plugins/mitosis-orchestrator
```

### 方式二：透過 `agy plugin` CLI 安裝

```bash
git clone https://github.com/ivanusto/mitosis-orchestrator.git ~/mitosis-orchestrator
agy plugin validate ~/mitosis-orchestrator
agy plugin install ~/mitosis-orchestrator
```

安裝後請重新啟動 `agy`。可隨時透過 `/plugin` 介面或 `agy plugin enable mitosis-orchestrator` / `agy plugin disable mitosis-orchestrator` 啟用或停用。

---

## 開發與測試

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```

**Headless 限制。** 在 `agy -p`（print）模式下，Antigravity 會把所有需要權限提示的工具直接軟性拒絕，不管 hook 回什麼 decision 或 `permissionOverrides`；該模式只認 `settings.json` 的 allow 規則或 `--dangerously-skip-permissions`。因此工作區範圍守衛只在互動式 session 中能減少提示（已在 agy 1.4.0 互動模式驗證：工作區內的編輯與測試命令不再跳出權限詢問）。

已於 2026-10-16 在 `agy` 1.4.0 實測：`SessionStart` 與 `PreInvocation` 提示確實進入模型上下文；實際模型 id 形如 `gemini-4-argon-high` / `gemini-3.8-flash-high`；`Stop` 會收到 `fullyIdle`、`executionNum` 與 `terminationReason`（正常結束回合為 `NO_TOOL_CALL`）；子代理中繼資料在 `manage_subagents(Action="kill")` 後由 `SUBAGENT_STATE_ALIVE` 變為 `SUBAGENT_STATE_KILLED`。

版本紀錄見 [CHANGELOG.md](./CHANGELOG.md)。

---

## 授權

[MIT](./LICENSE)
