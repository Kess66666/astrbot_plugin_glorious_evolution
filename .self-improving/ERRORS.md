# Errors

Command failures and integration errors.

---

## [ERR-20260510-001] dev_write_file 部分覆写导致 evolution_task.py 结构丢失 → IndentationError

**Logged**: 2026-05-10T10:45:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: infra

### Summary
使用 `dev_write_file` 只写入函数体片段（查重逻辑），未保留文件头（imports/class定义），导致 `evolution_task.py` 只剩半截函数体。Python 解析器从第1行崩：`IndentationError: unexpected indent`。插件加载链击穿，所有插件被连坐失效。

### 根因
`dev_write_file` 是全量覆写工具，传入非完整文件内容 = 文件截断。属于**工具选择错误**——应用 `astrbot_file_edit_tool` 做精准 diff 替换。

### 修复
1. `git checkout HEAD -- evolution_task.py` 恢复完整文件
2. 用 `astrbot_file_edit_tool` 重新插入查重逻辑
3. `python3 -c "compile(...)"` 语法验证通过
4. `dev_load_plugin` 重新加载

### 制度修复
CLAUDE.md 编码红线新增 **SAFE FILE EDIT POLICY**：核心文件永不用 `dev_write_file`。

### Metadata
- Reproducible: yes (dev_write_file + 非完整内容 = 100% 触发)
- Related Files: evolution_task.py, CLAUDE.md
- Source: reload 日志
- See Also: SAFE FILE EDIT POLICY (CLAUDE.md)

---

## [ERR-20260509-001] memory_manager.py:94 `\n` escape rendered as literal newline

**Logged**: 2026-05-09T21:38:00+08:00
**Priority**: high
**Status**: resolved
**Area**: coding

### Summary
插件重载报 `SyntaxError: unterminated string literal (detected at line 94)`。`memory_manager.py` 第 94 行 `text = entry.question + "\n" + entry.content` 中的 `\n` 转义序列被渲染为物理换行符，导致字符串断裂。

### 根因
文件编辑工具（`astrbot_file_edit_tool`）在前次修改时，将 `\n` 的 old/new 匹配字符串中的反斜杠+字母 n 错误替换为 ASCII 0x0A 换行符。编辑工具匹配时无法区分「字符串内的转义序列」和「真实的源码换行」，属于工具层面的转义陷阱。

### 修复
使用 Python 脚本直接读写字节流，`replace('"\n"', '"\\n"')` 将物理换行恢复为 `\n` 转义序列。编辑工具匹配失败后用 `sed` / `astrbot_execute_python` 兜底。

### 模式关联
- CLAUDE.md 编码红线第 4 条已预警（「编辑含转义字符的字符串时，禁止直接用文件编辑工具替换」）
- 同类根因：LRN-20260502-001（rf-string 反斜杠问题）
- **建议**：后续编辑含 `\n`、`\t`、`\\` 等转义序列的代码时，优先用 `astrbot_execute_python` 整段重写，或使用 `astrbot_file_write_tool` 写入完整文件

### Metadata
- Reproducible: yes (file edit tool + escape sequence = 100% 触发)
- Related Files: memory_manager.py L94
- Source: reload 日志
- See Also: CLAUDE.md 编码红线, LRN-20260502-001

---

## [ERR-20260507-001] 97% memories stuck in pending — 反馈闭环断裂

**Logged**: 2026-05-07T23:00:00+08:00
**Priority**: critical
**Status**: resolved (v1.0.33 T-004 Soft Feedback)
**Area**: core

### Summary
67/70 memories 停留在 pending 状态，win_rate 统计完全失效。根本原因是 feedback 闭环仅存在于 `run_agent_loop`（手动 Tool），普通对话路径（`on_llm_request → _inject_relevant_memories`）从未调用 `_record_feedback`。

### 根因
- `update_win_rate` Tool 需要 LLM 主动调用 → 几乎从不触发
- `run_agent_loop` 的 judge 阶段会记录反馈，但普通对话不走这个路径
- `_inject_relevant_memories` 注入了记忆但没有后续反馈步骤

### Resolution
- **Resolved**: 2026-05-07 (v1.0.33)
- **Fix**: `_inject_relevant_memories` 出口加 `asyncio.create_task(self._soft_feedback(injected_ids))`
- **Design**: 默认标记注入记忆为成功（远优于 97% pending 空转），零 Token 成本
- **Verified**: pending 67→62, correct 5→10 在第一次对话中生效

### Metadata
- Reproducible: yes (every normal conversation)
- Related Files: main.py (`_inject_relevant_memories`, `_soft_feedback`)
- Source: conversation + Gemini 协作
- See Also: MEM-20260507-114, LRN-20260507-001

## [ERR-20260504-003] on_llm_request 记忆注入从未生效

**Logged**: 2026-05-04T21:12:00+08:00
**Priority**: critical
**Status**: resolved (v1.0.22 三级 fallback + v1.0.28 能力感知路由)
**Area**: core

### Summary
`_inject_relevant_memories(req)` 在 Internal Agent 模式下从未执行过注入逻辑，所有"记忆注入生效"的体感实际来自 LLM 主动调用 `store_memory`/`search_memory` Tool。

### 根因
AstrBot Internal Agent 模式下 `ProviderRequest.prompt = ""`（空字符串），用户消息通过 `req.contexts`（对话历史列表）传递。

代码路径：
- `astr_main_agent.py:1144-1145`: `req = ProviderRequest(); req.prompt = ""`
- `main.py:741`: `if len(prompt) <= 5: return` — 空字符串直接 return

### 历史
1. **v1 期**: 用 `@filter.on_agent_begin()` → 该 hook 签名是 `(event, run_context)`，没有 `ProviderRequest`，无法注入
2. **v2 期**: 改回 `@filter.on_llm_request()` → 有 `req` 但 `req.prompt=""`，guard 挡掉

### 修复方向
- **已采用方案（备选方案改进）**: `_inject_relevant_memories` 签名不变，query 提取使用三级 fallback 链: `req.prompt` → `event.message_str` → `req.contexts` 最后一条 user message
- **v1.0.28**: 引入能力感知路由 — `has_tools = getattr(req, "func_tool", None) is not None`，按 agent 能力分流注入规则，无工具 agent 不注入 TOOL GATE
- 参考: Angel Memory (kawayiYokami/astrbot_plugin_angel_memory) 把 event 传给下游，从 `event.message_str` 取 query

### Resolution
- **Resolved**: 2026-05-06 (v1.0.22 修复 query 提取 + v1.0.28 能力感知路由)
- **Notes**: 单钩子方案足够，未采用两段式 on_agent_begin + on_llm_request

### Metadata
- Reproducible: yes (every LLM call in Internal Agent mode)
- Related Files: main.py (L736-800)
- Source: code review + Angel Memory 对比
- See Also: LRN-20260504-002

---

## [ERR-20260502-002] dev_uninstall_plugin 误删插件（复发性）

**Logged**: 2026-05-02T21:23:00+08:00
**Priority**: critical
**Status**: resolved (v1.0.23+，dev_uninstall_plugin 已从工具集移除)
**Area**: infra

### Summary
door 多次使用 `dev_uninstall_plugin` 误删插件目录，导致未提交代码丢失。已 2 次记录在案。

### 历史
1. **2026-04-27**: v0.2.0 代码因"误操作"丢失，插件回退到 v0.1。此事件促成了 TOOLS.md 的创建。
2. **2026-05-02**: 插件目录被删，未提交的 `set_distillation_config` 修复丢失，蒸馏管线功能崩溃（用户指出："这就是你错误删除插件带来的问题"）。

### 根因
- 混淆了 `dev_uninstall_plugin`（卸载）与"重载"的概念
- TOOLS.md 已明文禁止，但未被严格遵循

### Resolution
- **Resolved**: 2026-05-02T21:25:00+08:00
- **Fix**: 直接禁用 `dev_uninstall_plugin` 工具，从根上杜绝误用
- **Notes**: 物理隔离 > 规则提醒，不再依赖 TOOLS.md 的"自觉遵守"

### Metadata
- Reproducible: yes
- Related Files: TOOLS.md, REBUILD_PLAN.md, main.py
- Source: conversation
- Recurrence-Count: 2
- First-Seen: 2026-04-27
- Last-Seen: 2026-05-02
- See Also: —

---

## [ERR-20260502-001] tool_sanitizer.py rf-string parse

**Logged**: 2026-05-02T21:21:00+08:00
**Priority**: high
**Status**: resolved
**Area**: coding

### Summary
Python 3.12+ 无法解析 `tool_sanitizer.py:165` 的 rf-string：正则反斜杠 \s 在 f-string 表达式中触发语法错误，导致插件加载失败。

### Error
```
SyntaxError: f-string expression part cannot include a backslash
```
位置：`tool_sanitizer.py` 第 164-165 行，4 处 rf-string 混用正则反斜杠模式。

### Context
- Glorious Evolution 插件 v1.0.16 加载时崩溃
- Claude 定位到 `tool_sanitizer.py` 第 165 行 `rf"('{key_pattern_str}')\s*:\s*'([^']+)'"` 
- 同类错误此前在 `reasoning_engine.py` 出现过（0dcac24 修复）

### Suggested Fix
4 处 rf-string 模式全部改为字符串变量拼接：
- `_pat1` = `'"' + key + '")\\s*:\\s*"([^"]+)"'`
- `_pat2` = `"'" + key + "')\\s*:\\s*'([^']+)'"`
- `_pat3` = `'"' + key + '")\\s*:\\s*(\\S+)'`
- `_cli_pat` = `'--(' + cli_key + ')=(\\S+)'`

### Resolution
- **Resolved**: 2026-05-02T21:18:00+08:00
- **Commit/PR**: efd9eb6 on Kess66666/astrbot_plugin_glorious_evolution
- **Notes**: 4 处 rf-string 正则模式均改为变量拼接，与 reasoning_engine.py 修法一致

### Metadata
- Reproducible: yes
- Related Files: tool_sanitizer.py, reasoning_engine.py
- See Also: LRN-20260502-001
