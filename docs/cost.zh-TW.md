# Claude Code 主核模型選擇：Fable 5.1 與 Opus 5.5

價格與量測數據截至 2026-10-22，使用前請查官方價目表。

[English](./cost.md) · [回到 README](../README.zh-TW.md)

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
python3 scripts/mitosis-cost-report.py --format ccusage    # 格式同 `ccusage session --json`（ccusage 20.x）
python3 scripts/mitosis-cost-report.py --debug-log /tmp/mitosis-orchestrator/<session>/mitosis_orchestrator_debug.log
```

1.7.0 起報表多一行節省金額：把每筆子代理列改用主核模型重新計價（全尖端模型的反事實），再與實際釘定的偵察者與執行者費用比較。`--format text|json|ccusage` 選擇輸出格式（`--json` 仍是別名）。若存在 hook 除錯日誌（`*_MITOSIS_DEBUG=1`，自動依 session 尋找，或用 `--debug-log` 指定），報表會多一段路由統計，列出各階層的決策數與觸發的規則。

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
