# Errors

Command failures and integration errors.

---

## [ERR-20260502-002] dev_uninstall_plugin 误删插件（复发性）

**Logged**: 2026-05-02T21:23:00+08:00
**Priority**: critical
**Status**: pending
**Area**: infra

### Summary
door 多次使用 `dev_uninstall_plugin` 误删插件目录，导致未提交代码丢失。已 2 次记录在案。

### 历史
1. **2026-04-27**: v0.2.0 代码因"误操作"丢失，插件回退到 v0.1。此事件促成了 TOOLS.md 的创建。
2. **2026-05-02**: 插件目录被删，未提交的 `set_distillation_config` 修复丢失，蒸馏管线功能崩溃（用户指出："这就是你错误删除插件带来的问题"）。

### 根因
- 混淆了 `dev_uninstall_plugin`（卸载）与"重载"的概念
- TOOLS.md 已明文禁止，但未被严格遵循

### Suggested Fix
- 门禁规则：**任何修改完成后直接推 GitHub，不留未提交变更在本地**
- 重载插件只用：修改文件 → 框架自动热重载；或 `dev_load_plugin` 装载未安装插件
- 每次对话开始检查 TOOLS.md 的禁令清单

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

---
