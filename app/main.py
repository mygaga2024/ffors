"""
FFORS FastAPI 应用入口
- 生命周期管理（启动时初始化数据库连接池）
- CORS 中间件配置
- API v1 路由注册
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.models import init_db
from app.api.v1 import rates as rates_router
from app.api.v1 import vendors as vendors_router
from app.api.v1 import ports as ports_router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.utils.logger import get_logger

logger = get_logger("ffors.main")


# ─────────────────────────────────────────────
# 生命周期管理
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动与关闭生命周期。"""
    logger.info("FFORS 服务启动中...")
    await init_db()
    logger.info("数据库连接池初始化完成")
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("FFORS 服务关闭")


# ─────────────────────────────────────────────
# FastAPI 应用实例
# ─────────────────────────────────────────────

app = FastAPI(
    title="FFORS — Freight Forwarder Ocean Rate System",
    description="货代海运报价管理系统 API",
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ─────────────────────────────────────────────
# CORS 中间件
# ─────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 生产环境应限制为前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# 路由注册
# ─────────────────────────────────────────────

app.include_router(rates_router.router, prefix="/api/v1")
app.include_router(vendors_router.router, prefix="/api/v1")
app.include_router(ports_router.router, prefix="/api/v1")


# ─────────────────────────────────────────────
# 健康检查端点
# ─────────────────────────────────────────────

@app.get("/health", tags=["system"], summary="健康检查")
async def health_check():
    return {"status": "ok", "service": "ffors"}
