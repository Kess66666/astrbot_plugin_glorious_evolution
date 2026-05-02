# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

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
