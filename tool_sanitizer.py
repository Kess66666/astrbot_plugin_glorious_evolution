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
    """保留前缀和末尾字符，中间用 **** 替换。
    
    例如: 'sk-abc123def456' -> 'sk-****ef456'
    """
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
    """脱敏 Key=Value 格式的行，保留 key 名，替换 value 为 ****。"""
    key = match.group(1)
    separator = match.group(2)
    value = match.group(3)
    # 如果 value 已经很短或为空，不脱敏
    if not value or len(value) <= 2:
        return match.group(0)
    return f"{key}{separator}****"


def _has_context_keyword(text: str, start: int, end: int) -> bool:
    """检查匹配位置附近是否存在上下文关键词。"""
    # 检查前后各100个字符
    context_start = max(0, start - 100)
    context_end = min(len(text), end + 100)
    context = text[context_start:context_end].lower()
    return any(kw in context for kw in CONTEXT_KEYWORDS)


def _is_excluded_pattern(text: str, start: int, end: int) -> bool:
    """判断匹配位置是否属于需要排除的模式（URL、路径、UUID、SHA-256）。"""
    # 检查是否是 UUID
    for m in _UUID_RE.finditer(text):
        if m.start() <= start and m.end() >= end:
            return True
    # 检查是否是 SHA-256
    for m in _SHA256_RE.finditer(text):
        if m.start() <= start and m.end() >= end:
            return True
    # 检查是否包含 URL 特征
    match_text = text[max(0, start - 10):min(len(text), end + 10)]
    if re.search(r'https?://|ftp://', match_text):
        return True
    # 检查是否包含文件路径特征（Unix 路径或 Windows 路径）
    if re.search(r'[/\\][\w.]+[/\\]', match_text):
        return True
    return False


# ==================== 脱敏规则（按优先级排序） ====================

def _sanitize_rules_priority1_sk(match: re.Match) -> str:
    """规则1: sk-[A-Za-z0-9]{32,} -> sk-****...****{后4位}"""
    value = match.group(0)
    if len(value) <= 8:
        return value
    return "sk-" + "****" + value[-4:]


def _sanitize_rules_priority2_github(match: re.Match) -> str:
    """规则2: gh[pous]_[A-Za-z0-9]{36,} -> gh?_****...****{后4位}"""
    value = match.group(0)
    if len(value) <= 10:
        return value
    prefix = value.split("_")[0] + "_"
    return prefix + "****" + value[-4:]


def _sanitize_rules_priority3_bearer(match: re.Match) -> str:
    """规则3: Bearer [A-Za-z0-9_\\-\\.]{20,} -> Bearer ****"""
    return "Bearer ****"


def _sanitize_rules_priority7_long_token(match: re.Match) -> str:
    """规则7: 长度 > 30 的纯字母数字+特殊字符连续串（需上下文触发）"""
    value = match.group(0)
    start, end = match.start(), match.end()
    
    # 排除误杀
    if _is_excluded_pattern(match.string, start, end):
        return value
    
    # 必须有上下文关键词才触发
    if not _has_context_keyword(match.string, start, end):
        return value
    
    if len(value) <= 8:
        return value
    return value[:4] + "****" + value[-4:]


# ==================== 主脱敏函数 ====================

def sanitize_content(text: str) -> str:
    """对文本中的敏感信息进行脱敏处理。
    
    脱敏规则（按优先级）：
    1. sk-xxx (OpenAI API Key)
    2. gh[pous]_xxx (GitHub Token)
    3. Bearer xxx
    4. Key=Value 行中的敏感 key
    5. JSON 中的敏感 key
    6. CLI 参数 --password= / --token= 等
    7. 上下文含敏感关键词时的长 token 串
    
    Args:
        text: 待脱敏的文本
        
    Returns:
        脱敏后的文本
    """
    if not ENABLE_SANITIZATION or not text:
        return text
    
    result = text
    
    # 规则1: OpenAI 风格 API Key - sk-[A-Za-z0-9]{32,}
    result = re.sub(
        r'sk-[A-Za-z0-9]{32,}',
        _sanitize_rules_priority1_sk,
        result
    )
    
    # 规则2: GitHub Token 风格 - gh[pous]_[A-Za-z0-9]{36,}
    result = re.sub(
        r'gh[pous]_[A-Za-z0-9]{36,}',
        _sanitize_rules_priority2_github,
        result
    )
    
    # 规则3: Bearer Token - Bearer [A-Za-z0-9_\-\.]{20,}
    result = re.sub(
        r'Bearer\s+[A-Za-z0-9_\-\.]{20,}',
        _sanitize_rules_priority3_bearer,
        result
    )
    
    # 构建敏感 key 的正则模式
    key_pattern_str = "|".join(SENSITIVE_KEY_PATTERNS)
    
    # 规则4: Key=Value 格式（等号分隔）
    result = re.sub(
        rf'({key_pattern_str})(\s*=\s*)(\S+)',
        _mask_key_value_line,
        result,
        flags=re.IGNORECASE
    )
    
    # 规则5: JSON 格式 - "key": "value"
    result = re.sub(
        rf'("{key_pattern_str}")\s*:\s*"([^"]+)"',
        lambda m: f'{m.group(1)}: "****"',
        result,
        flags=re.IGNORECASE
    )
    
    # 规则5b: JSON 格式 - "key": 'value'
    result = re.sub(
        rf"('{key_pattern_str}')\s*:\s*'([^']+)'",
        lambda m: f"{m.group(1)}: '****'",
        result,
        flags=re.IGNORECASE
    )
    
    # 规则5c: JSON 格式 - "key": value (无引号的值)
    result = re.sub(
        rf'("{key_pattern_str}")\s*:\s*(\S+)',
        lambda m: f'{m.group(1)}: ****',
        result,
        flags=re.IGNORECASE
    )
    
    # 规则6: CLI 参数 --password=xxx / --token=xxx 等
    cli_key_pattern = r'password|passwd|pwd|token|secret|key|auth|credential'
    result = re.sub(
        rf'--({cli_key_pattern})=(\S+)',
        lambda m: f'--{m.group(1)}=****',
        result,
        flags=re.IGNORECASE
    )
    
    # 规则7: 长度 > 30 的连续字母数字+特殊字符（需上下文触发）
    # 排除明显不是 token 的模式（URL、路径已排除）
    result = re.sub(
        r'\b[A-Za-z0-9_\-\.]{31,}\b',
        _sanitize_rules_priority7_long_token,
        result
    )
    
    return result


def sanitize_tool_output(tool_name: str, content: str) -> str:
    """对特定工具的返回值做更严格的脱敏。
    
    对 dev_read_file、fetch_url、query_repository 等工具做严格脱敏，
    其他工具只做宽松脱敏。
    
    Args:
        tool_name: 工具名称
        content: 工具返回的内容
        
    Returns:
        脱敏后的内容
    """
    if not ENABLE_SANITIZATION or not content:
        return content
    
    # 先做基础脱敏
    result = sanitize_content(content)
    
    # 严格模式：对特定工具追加脱敏规则
    if tool_name in STRICT_TOOL_NAMES:
        # 额外脱敏任何看起来像 token 的长字符串（>= 20字符）
        # 这些工具返回的内容中更可能包含敏感信息
        result = re.sub(
            r'(?<!\w)[A-Za-z0-9_\-\.]{20,}(?!\w)',
            lambda m: "****" if not _is_excluded_pattern(result, m.start(), m.end()) else m.group(0),
            result
        )
        
        # 脱敏 email（可能包含在配置文件中）
        result = re.sub(
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            '****@****.***',
            result
        )
    
    return result


# ==================== 测试块 ====================
if __name__ == "__main__":
    # 测试用例
    test_cases = [
        # 基础 API Key 脱敏
        (
            "OpenAI Key: sk-abcdefghijklmnopqrstuvwxyz12345678901234",
            "应脱敏 OpenAI Key"
        ),
        # GitHub Token
        (
            "Token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn",
            "应脱敏 GitHub Personal Token"
        ),
        # Bearer Token
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "应脱敏 Bearer Token"
        ),
        # Key=Value 格式
        (
            "api_key=sk-supersecretkey123456789012345678901234",
            "应脱敏 Key=Value"
        ),
        # JSON 格式
        (
            '{"api_key": "sk-secretvalue123456789012345678901234567890"}',
            "应脱敏 JSON"
        ),
        # CLI 参数
        (
            "--password=mysecretpassword12345678901234",
            "应脱敏 CLI 参数"
        ),
        # 不应脱敏的内容
        (
            "File path: /home/user/config.json",
            "不应脱敏文件路径"
        ),
        (
            "Visit https://example.com/api/docs",
            "不应脱敏 URL"
        ),
        (
            "UUID: 550e8400-e29b-41d4-a716-446655440000",
            "不应脱敏 UUID"
        ),
        (
            "SHA256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "不应脱敏 SHA-256"
        ),
        # 上下文触发的长 token
        (
            "auth_token: ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghijklmnop",
            "上下文含 auth_token，应脱敏长 token"
        ),
        # 混合场景
        (
            "Debug: token=ghr_abcdefghijklmnopqrstuvwxyz12345678901234567890\n"
            "Path: /var/log/app.log\n"
            "api_key: sk-test1234567890123456789012345678901234",
            "混合场景测试"
        ),
    ]
    
    print("=" * 60)
    print("Tool Sanitizer 测试")
    print("=" * 60)
    
    for i, (text, desc) in enumerate(test_cases, 1):
        print(f"\n--- 测试 {i}: {desc} ---")
        print(f"输入: {text}")
        sanitized = sanitize_content(text)
        print(f"输出: {sanitized}")
    
    # 测试 sanitize_tool_output
    print("\n" + "=" * 60)
    print("sanitize_tool_output 测试")
    print("=" * 60)
    
    tool_test = (
        "文件内容:\n"
        'OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz12345678901234\n'
        "访问 https://api.openai.com/v1/chat\n"
        "管理员邮箱: admin@example.com\n"
        "UUID: 550e8400-e29b-41d4-a716-446655440000"
    )
    
    print(f"\n--- dev_read_file (严格模式) ---")
    print(f"输入:\n{tool_test}")
    print(f"\n输出:\n{sanitize_tool_output('dev_read_file', tool_test)}")
    
    print(f"\n--- default_tool (宽松模式) ---")
    print(f"输出:\n{sanitize_tool_output('default_tool', tool_test)}")
    
    print("\n" + "=" * 60)
    print("测试完成")
