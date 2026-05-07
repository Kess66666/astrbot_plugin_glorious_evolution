# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260504-002] knowledge_gap — AstrBot Internal Agent 模式下 ProviderRequest.prompt 为空

**Logged**: 2026-05-04T21:12:00+08:00
**Priority**: critical
**Status**: pending
**Area**: coding

### Summary
AstrBot Internal Agent 模式下，`ProviderRequest.prompt` 始终为空字符串 `""`。用户消息不在这个字段里，而是通过 `req.contexts`（对话历史列表，最后一条 role=user 的消息）传递。此外 `event.message_str` 也能拿到用户原始输入。

### 三个 Hook 的 ProviderRequest 访问对比
| Hook | 参数 | 能否拿到 req | prompt 状态 |
|---|---|---|---|
| `on_llm_request` | event, req | ✅ 直接 | `""` (Internal Agent) |
| `on_agent_begin` | event, run_context | ❌ 无 req | N/A |
| `on_llm_response` | event, resp | ❌ 无 req | N/A |

### 正确的 query 提取方式
```python
query = req.prompt or ""
if len(query) <= 5:
    query = getattr(event, "message_str", "") or ""
if len(query) <= 5:
    for msg in reversed(req.contexts or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 5:
                query = content
                break
```

### 三个 Hook 的职责分工（2026-05-05 修正）

旧结论"不要用 on_agent_begin 做记忆注入"是**片面且误导的**。正确理解：

| Hook | 适合做什么 | 不适合做什么 |
|---|---|---|
| `on_agent_begin` | 记忆检索（用 event.message_str 做 query，结果存 event.set_extra） | 直接注入 system_prompt（无 ProviderRequest） |
| `on_llm_request` | 记忆注入（从 event.get_extra 读检索结果 → req.system_prompt +=） | 检索（每轮 tool 迭代都触发，效率低） |

### 两段式方案（已评估，未采用 — 单钩子 + fallback 方案已足够）
1. **on_agent_begin**: 用 `event.message_str` 检索记忆 → `event.set_extra("_ge_memories", data)`
2. **on_llm_request**: 从 `event.get_extra("_ge_memories")` 读取 → 注入 `req.system_prompt`
3. 签名须含 event：`async def on_agent_begin(self, event: AstrMessageEvent, run_context)` — 少 event 会 TypeError 参数错位（已踩坑验证）

### 实际采用方案（v1.0.22）
- 单钩子 `on_llm_request` + 三级 query fallback: `req.prompt` → `event.message_str` → `req.contexts`
- v1.0.28 在此基础上引入能力感知路由，按 agent 工具能力分流注入规则
- 单钩子方案代码更简洁、无状态同步问题、维护成本更低

### 行动项
- 实现两段式方案，解耦检索和注入
- 查 AstrBot API 前必须先加载 skill-astrbot-dev → 读官方文档 → 再翻源码补充（不要反过来）

### Metadata
- Source: code review + Angel Memory (kawayiYokami/astrbot_plugin_angel_memory) 对比
- Related Files: main.py (L736-800)
- Tags: astrbot, provider-request, internal-agent, prompt-empty
- Pattern-Key: astrbot.internal_agent.prompt_empty
- First-Seen: 2026-05-04
- See Also: ERR-20260504-003

---

## [LRN-20260502-001] best_practice

**Logged**: 2026-05-02T21:21:00+08:00
**Priority**: high
**Status**: pending
**Area**: coding

### Summary
Python 3.12+ rf-string 内混用正则反斜杠（\s, \S, \d, \w）会触发解析歧义，导致 f-string 语法错误。应先用字符串拼接构造模式，再传给 re.sub。

### Details
两次在 Glorious Evolution 插件中触发同一问题：
1. `reasoning_engine.py` — `rf"(\d+\.\s\*\*)"` 等模式，由 `0dcac24` 修复
2. `tool_sanitizer.py` — `rf'("{key}")\\s*:\\s*"([^"]+)"'` 等 4 处，由 `efd9eb6` 修复

根本原因：Python 3.12 起 f-string 内不允许反斜杠出现在表达式部分。在 rf-string 中，正则元字符（\s, \S, \*, +）会让解析器误判为 f-string 转义，即便外层有 r 前缀也无法豁免。

### Suggested Action
**编写代码时**：凡是包含正则反斜杠（\s \S \d \w \b \\）的模式，一律用字符串拼接而非 rf-string：
```python
# ❌ 危险 — Python 3.12+ 可能报错
pattern = rf'({var})\s*:\s*(\S+)'

# ✅ 安全 — 先用拼接构造
pattern = '(' + var + ')\\s*:\\s*(\\S+)'
```

### Metadata
- Source: error
- Related Files: tool_sanitizer.py, reasoning_engine.py
- Tags: python, f-string, regex, backslash, syntax-error
- Pattern-Key: harden.rf_string_regex_backslash
- Recurrence-Count: 2
- First-Seen: 2026-05-02
- Last-Seen: 2026-05-02
- See Also: ERR-20260502-001

---
