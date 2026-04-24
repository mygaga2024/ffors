"""
FFORS 统一日志模块
- 输出到 /logs/ffors.log，带日期轮转
- 敏感信息过滤器（遵循 DEVELOPMENT_PROTOCOL.md §2）
- 禁止打印客户名称、真实价格明细等敏感字段
"""

import logging
import os
import re
from logging.handlers import TimedRotatingFileHandler

from app.config import settings

# --- 敏感信息过滤器 ---
_SENSITIVE_PATTERNS = [
    re.compile(r"(api[_-]?key|token|password|secret|webhook)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]


class SensitiveDataFilter(logging.Filter):
    """过滤日志中的敏感字段，防止凭据泄露到日志文件。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(msg):
                record.msg = pattern.sub(r"\1: [REDACTED]", str(record.msg))
                record.args = ()
        return True


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_logger(name: str = "ffors") -> logging.Logger:
    """
    获取统一格式的 Logger 实例。
    同时输出到 控制台（INFO+）和 日志文件（DEBUG+，带日轮转，保留 30 天）。
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = _build_formatter()
    sensitive_filter = SensitiveDataFilter()

    # --- 控制台 Handler ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)
    logger.addHandler(console_handler)

    # --- 文件 Handler（日轮转） ---
    log_dir = settings.log_dir
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "ffors.log")

    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(sensitive_filter)
    logger.addHandler(file_handler)

    return logger


# 模块级默认 logger
logger = get_logger("ffors")
