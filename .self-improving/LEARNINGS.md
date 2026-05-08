# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260507-001] best_practice — 软反馈闭环：绕过 LLM 判断的异步反馈激活

**Logged**: 2026-05-07T23:00:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: architecture

### 问题
记忆 feedback 闭环完全依赖 LLM 手动调用 Tool → 97% pending。Gemini 建议在 `on_llm_request` Hook 里直接异步标记注入的记忆为成功。

### 方案
在 `_inject_relevant_memories` 最后加：
```python
if injected_ids:
    asyncio.create_task(self._soft_feedback(injected_ids))
```
`_soft_feedback` 等 10 秒后逐条 `update_win_rate(mid, True)`。

### 核心洞察
- **默认成功优于全 pending**：5% 误判风险 < 97% 空转危害
- **零 Token 成本**：不调 LLM，纯写库
- **瞬间激活**：重启后每说一句话就开始消化 pending 队列
- **配合 ChatGPT 方案 A**：agent_loop 的精确判断 + 软反馈的广度覆盖 = 双轨并行

---

## [LRN-20260508-003] best_practice — 分类器修复：LLM 驱动的记忆分类需要硬性规则，不能靠 LLM 自由裁量

**Logged**: 2026-05-08T22:10:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: architecture

### 问题
所有 `Insight:` 记忆被 LLM 无脑扔进 `declarative`，SQL 结果显示 `insight total: 0`。Gemini 诊断为"分类残疾"——LLM 不知道 `insight` 该往哪放。

### 根因
`classify_memory()` 的 prompt 只定义了 procedural/declarative/episodic 三种类型，未给 `insight` 和 `consolidated_rule` 下定义。LLM 面对 `memory_type: "insight"` 字段时只能猜，默认退回 declarative。

### 方案
1. **Prompt 补全**: 定义 insight = 系统诊断/胜率分布/病灶识别，consolidated_rule = 固化规则/最佳实践
2. **加硬性规则**: "如果内容是对系统自身状态的诊断分析或改进建议 → 必须是 insight，禁止归为 declarative"
3. **存量清洗**: `UPDATE memories SET memory_type='insight' WHERE question LIKE 'Insight:%'` — 19 条出土

### 核心洞察
- **LLM 分类器需要负面约束**：不仅要告诉它"什么是 X"，还要告诉它"什么情况下绝不能用 Y"
- **存量清洗是分类器修复的必要步骤**：prompt 只改未来，不改过去
- **结果**: insight avg_wr=59.1%，证明这些"元分析"记忆质量高于均值

### Metadata
- Source: Gemini 数据审计 + SQL 验证
- Related Files: main.py (classify_memory)
- Tags: classifier, insight, memory-type, prompt-engineering
- See Also: MEM-20260507-112, MEM-20260506-082
- Pattern-Key: GE.classifier.insight_definition

## [LRN-20260508-004] best_practice — v2.0 无偏检索 + 分层注入：检索与排序解耦

**Logged**: 2026-05-08T22:15:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: architecture

### 问题
v1.0.31 的三维评分 (0.6cos+0.25wr+0.15rec) 在检索阶段就引入 win_rate 偏见，导致冷门高相关记忆被 wr 高的老记忆碾压。MEM-20260507-112 用精准语义诱导才勉强挤进 top-20。

### 方案（Gemini + ChatGPT 共识）
**两阶段设计**：
1. **检索阶段 — 纯余弦 (cos=1.0, wr=0.0, rec=0.15)**：公平海选 top-20，保证相关性优先
2. **注入阶段 — 分三桶**：
   - exploit (wr > 0.7): top-2 + shuffle → 验证已知好记忆
   - explore (0.4 ≥ wr ≤ 0.7): top-2 + shuffle → 探索灰色地带
   - cold (wr < 0.4): 1条 random → 给低分记忆"试镜"机会

### 核心洞察
- **检索与排序必须解耦**：检索的目标是"找到所有可能相关的"，排序/注入的目标是"选最有价值的去呈现"
- **偏置保留在最后阶段**：在 5 条最终注入中做分层，而不是在 20 条候选里就排掉冷门
- **验证**: MEM-20260507-112 (usage 85→86) 被精准命中，v2.0 检索通过

### Metadata
- Source: Gemini 审计 + ChatGPT 共识
- Related Files: main.py (_soft_feedback v2, _inject_relevant_memories)
- Tags: stratified-injection, unbiased-retrieval, two-phase, v2.0

## [LRN-20260508-002] best_practice — v1.2 四刀流防御体系：从「行为约束」升级到「数据进化」

**Logged**: 2026-05-08T20:57:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: architecture

### 核心洞察
之前的 GE 在一个「没有反馈信号」的系统里跑淘汰算法 ≈ 用随机标签训练模型。97% pending、误删高质量记忆的根因不是 bug，是 selection pressure 不存在。

### v1.2 四刀流
1. **冷启动保护**: evolution_task.py 入口双 gate
2. **自动造血**: memory_manager.py 三个 retrieve 出口自动 usage++
3. **软反馈 0.7 上限**: main.py 的 _soft_feedback 加 win_rate 检查
4. **可追凶淘汰日志**: evict_low_quality 结构化 EVICT 日志

### 设计哲学（ChatGPT + Gemini 共识）
- 三层叠加：规则约束（防疯）→ 数据重构（造血）→ 混合排序（提效）
- 人才梯队：平民(50%)→骨干(70%)→核心(90%+)，0.7 以上需用户背书
- DISABLE_AUTO_EVOLUTION=True，仅手动触发进化

### Metadata
- Source: ChatGPT 评审 + Gemini 评审 + 飞书血案复盘
- Related Files: evolution_task.py, memory_manager.py, main.py
- Tags: v1.2, cold-start, auto-usage, soft-feedback-cap, eviction-logging
- See Also: LRN-20260507-001

### Metadata
- Source: conversation + Gemini 建议 + ChatGPT 方案 A 结合
- Related Files: main.py
- Tags: feedback-loop, soft-feedback, pending-resolution, t-004
- Pattern-Key: GE.soft_feedback.activation
- See Also: ERR-20260507-001, MEM-20260507-114

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
2. `tool_sanitizer.py` — `rf'(\"{key}\")\\s*:\\s*\"([^\"]+)\"'` 等 4 处，由 `efd9eb6` 修复

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
