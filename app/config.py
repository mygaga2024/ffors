"""
FFORS 全局配置模块
基于 pydantic-settings 从环境变量 / .env 文件读取配置，
严禁硬编码任何凭据 (DEVELOPMENT_PROTOCOL.md §2)。
"""

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置，所有字段均通过环境变量注入。"""

    model_config = SettingsConfigDict(
        env_file="env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Database ---
    database_url: str = "postgresql+asyncpg://ffors_user:password@localhost:5432/ffors"

    # --- AI Services — MiniMax ---
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_group_id: str = ""

    # --- AI Services — Gemini (备选) ---
    gemini_api_key: str = ""

    # --- AI Services — DeepSeek (补位) ---
    deepseek_api_key: str = ""

    # 通知配置 (WeCom Alert Bot)
    wecom_webhook_key: Optional[str] = None
    
    # 交互机器人配置 (Phase 3 Bot Webhook)
    dingtalk_app_secret: Optional[str] = None
    wecom_bot_token: Optional[str] = None
    wecom_encoding_aes_key: Optional[str] = None

    # --- Proxy ---
    http_proxy: str = ""
    https_proxy: str = ""

    # --- Paths ---
    data_dir: str = "/app/data"
    log_dir: str = "/app/logs"


# 全局单例，供各模块导入使用
settings = Settings()
