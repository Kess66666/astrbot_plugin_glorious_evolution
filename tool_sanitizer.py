"""
tool_sanitizer.py - 敏感信息脱敏工具

提供文本内容的敏感信息脱敏功能，防止 API Key、Token、密码等泄露。
所有替换操作使用 re.sub，无外部依赖。

导出：
- sanitize_content(text: str) -> str
- sanitize_tool_output(tool_name: str, content: str) -> str
- ENABLE_SANITIZATION: bool
"""

import re
from typing import List, Tuple, Pattern

# ==================== 配置开关 ====================
ENABLE_SANITIZATION = True

# ==================== 需要严格脱敏的工具列表 ====================
STRICT_TOOL_NAMES = frozenset({
    "dev_read_file",
    "fetch_url", 
    "query_repository",
    "astrbot_file_read_tool",
    "astrbot_grep_tool",
})

# ==================== 敏感 Key 名称（大小写不敏感匹配） ====================
SENSITIVE_KEY_PATTERNS = (
    "api_key", "apikey", "api-key",
    "token", "access_token", "refresh_token",
    "password", "passwd", "pwd",
    "secret", "secret_key", "client_secret",
    "auth", "authorization",
    "credential", "credentials",
    "github_token", "openai_api_key",
    "private_key", "ssh_key",
)

# ==================== 上下文关键词（用于规则7触发判断） ====================
CONTEXT_KEYWORDS = ("key", "token", "secret", "password", "auth", "credential")

# ==================== 误杀排除：UUID 和 SHA-256 哈希 ====================
_UUID_RE = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'
)
_SHA256_RE = re.compile(r'\b[0-9a-fA-F]{64}\b')


def _mask_middle(text: str, keep_end: int = 4) -> str:
    if len(text) <= keep_end + 4:
        return text
    prefix = text[:3] if text.startswith("sk-") or len(text.split("_")[0]) <= 3 else ""
    if prefix:
        remaining = text[len(prefix):]
        if len(remaining) <= keep_end:
            return text
        return prefix + "****" + remaining[-keep_end:]
    return "****" + text[-keep_end:]


def _mask_key_value_line(match: re.Match) -> str:
    key = match.group(1)
    separator = match.group(2)
    value = match.group(3)
    if not value or len(value) <= 2:
        return match.group(0)
    return f"{key}{separator}****"


def _has_context_keyword(text: str, start: int, end: int) -> bool:
    context_start = max(0, start - 100)
    context_end = min(len(text), end + 100)
    context = text[context_start:context_end].lower()
    return any(kw in context for kw in CONTEXT_KEYWORDS)


def _is_excluded_pattern(text: str, start: int, end: int) -> bool:
    for m in _UUID_RE.finditer(text):
        if m.start() <= start and m.end() >= end:
            return True
    for m in _SHA256_RE.finditer(text):
        if m.start() <= start and m.end() >= end:
            return True
    match_text = text[max(0, start - 10):min(len(text), end + 10)]
    if re.search(r'https?://|ftp://', match_text):
        return True
    if re.search(r'[/\\][\w.]+[/\\]', match_text):
        return True
    return False


def _sanitize_rules_priority1_sk(match: re.Match) -> str:
    value = match.group(0)
    if len(value) <= 8:
        return value
    return "sk-" + "****" + value[-4:]


def _sanitize_rules_priority2_github(match: re.Match) -> str:
    value = match.group(0)
    if len(value) <= 10:
        return value
    prefix = value.split("_")[0] + "_"
    return prefix + "****" + value[-4:]


def _sanitize_rules_priority3_bearer(match: re.Match) -> str:
    return "Bearer ****"


def _sanitize_rules_priority7_long_token(match: re.Match) -> str:
    value = match.group(0)
    start, end = match.start(), match.end()
    if _is_excluded_pattern(match.string, start, end):
        return value
    if not _has_context_keyword(match.string, start, end):
        return value
    if len(value) <= 8:
        return value
    return value[:4] + "****" + value[-4:]


def sanitize_content(text: str) -> str:
    if not ENABLE_SANITIZATION or not text:
        return text
    
    result = text
    
    result = re.sub(
        r'sk-[A-Za-z0-9]{32,}',
        _sanitize_rules_priority1_sk,
        result
    )
    
    result = re.sub(
        r'gh[pous]_[A-Za-z0-9]{36,}',
        _sanitize_rules_priority2_github,
        result
    )
    
    result = re.sub(
        r'Bearer\s+[A-Za-z0-9_\-\.]{20,}',
        _sanitize_rules_priority3_bearer,
        result
    )
    
    key_pattern_str = "|".join(SENSITIVE_KEY_PATTERNS)
    
    result = re.sub(
        rf'({key_pattern_str})(\s*=\s*)(\S+)',
        _mask_key_value_line,
        result,
        flags=re.IGNORECASE
    )
    
    result = re.sub(
        rf'("{key_pattern_str}")\s*:\s*"([^"]+)"',
        lambda m: f'{m.group(1)}: "****"',
        result,
        flags=re.IGNORECASE
    )
    
    result = re.sub(
        rf"('{key_pattern_str}')\s*:\s*'([^']+)',
        lambda m: f"{m.group(1)}: '****'",
        result,
        flags=re.IGNORECASE
    )
    
    result = re.sub(
        rf'("{key_pattern_str}")\s*:\s*(\S+)',
        lambda m: f'{m.group(1)}: ****',
        result,
        flags=re.IGNORECASE
    )
    
    cli_key_pattern = r'password|passwd|pwd|token|secret|key|auth|credential'
    result = re.sub(
        rf'--({cli_key_pattern})=(\S+)',
        lambda m: f'--{m.group(1)}=****',
        result,
        flags=re.IGNORECASE
    )
    
    result = re.sub(
        r'\b[A-Za-z0-9_\-\.]{31,}\b',
        _sanitize_rules_priority7_long_token,
        result
    )
    
    return result


def sanitize_tool_output(tool_name: str, content: str) -> str:
    if not ENABLE_SANITIZATION or not content:
        return content
    
    result = sanitize_content(content)
    
    if tool_name in STRICT_TOOL_NAMES:
        result = re.sub(
            r'(?<!\w)[A-Za-z0-9_\-\.]{20,}(?!\w)',
            lambda m: "****" if not _is_excluded_pattern(result, m.start(), m.end()) else m.group(0),
            result
        )
        
        result = re.sub(
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            '****@****.***',
            result
        )
    
    return result


if __name__ == "__main__":
    test_cases = [
        ("OpenAI Key: sk-abcdefghijklmnopqrstuvwxyz12345678901234", "应脱敏 OpenAI Key"),
        ("Token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn", "应脱敏 GitHub Personal Token"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "应脱敏 Bearer Token"),
        ("api_key=sk-supersecretkey123456789012345678901234", "应脱敏 Key=Value"),
        ('{"api_key": "sk-secretvalue123456789012345678901234567890"}', "应脱敏 JSON"),
        ("--password=mysecretpassword12345678901234", "应脱敏 CLI 参数"),
        ("File path: /home/user/config.json", "不应脱敏文件路径"),
        ("Visit https://example.com/api/docs", "不应脱敏 URL"),
        ("UUID: 550e8400-e29b-41d4-a716-446655440000", "不应脱敏 UUID"),
        ("SHA256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "不应脱敏 SHA-256"),
    ]
    print("=" * 60)
    print("Tool Sanitizer 测试")
    print("=" * 60)
    for i, (text, desc) in enumerate(test_cases, 1):
        print(f"\n--- 测试 {i}: {desc} ---")
        print(f"输入: {text}")
        print(f"输出: {sanitize_content(text)}")