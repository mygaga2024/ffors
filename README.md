# FFORS — Freight Forwarder Ocean Rate System

> 货代海运报价智能管理系统

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-blue)](https://postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue)](https://docs.docker.com/compose/)

## 系统简介
FFORS 是一套面向国际货代的海运报价管理平台，提供：

📊 报价数据管理 — Excel 批量导入、多维查询、航线报价追踪
📈 量化分析 — 环比 (WoW) / 月同比 (MoM) 自动计算 
🤖 AI 辅助 — MiniMax 大模型驱动的行情分析与摘要
🔔 企业微信通知 — 异常价格波动自动推送 

## 快速启动

### 前置要求

- [Docker](https://docs.docker.com/get-docker/) & [Docker Compose](https://docs.docker.com/compose/install/)
- Git

### 1. 克隆项目

```bash
git clone https://github.com/mygaga2024/ffors.git
cd ffors
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写数据库密码及其他配置
```

### 3. 启动服务

```bash
docker compose up -d
```

### 4. 验证服务

```bash
# 健康检查
curl http://localhost:8000/health

# 查看 API 文档
open http://localhost:8000/docs
```

## API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/v1/rates/` | 查询报价列表（支持多维过滤） |
| `GET` | `/api/v1/rates/{id}` | 查询单条报价详情 |
| `POST` | `/api/v1/rates/import/excel` | Excel 批量导入报价 |
| `GET` | `/docs` | Swagger API 文档 |
| `GET` | `/redoc` | ReDoc API 文档 |

## Excel 导入格式

上传 `.xlsx` 文件，列名使用英文：

| POL | POD | Carrier | 20GP | 40GP | 40HQ | Currency | ETD | TT(Days) | Valid From | Valid To | Remarks |
|-----|-----|---------|------|------|------|----------|-----|----------|------------|----------|---------|
| CNSHA | NLRTM | COSCO | 1200 | 1800 | 2000 | USD | 2025-01-15 | 28 | 2025-01-01 | 2025-01-31 | - |


## 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| Web 框架 | FastAPI | 异步高性能 Python Web 框架 |
| 数据库 | PostgreSQL 16 | 关系型数据库 |
| ORM | SQLAlchemy 2.0 (async) | 异步数据库操作 |
| 数据处理 | pandas + openpyxl | Excel 解析 |
| AI 引擎 | MiniMax / Gemini | 大模型分析 (Phase 2) |
| 通知 | 企业微信 Webhook | 异常推送 (Phase 2) |
| 部署 | Docker Compose | 容器化部署 |
| 目标环境 | 绿联 DXP4800 Plus NAS | x86_64 / UGOS Pro |

## NAS 部署说明
1. 使用 `linux/amd64` 架构镜像
2. PostgreSQL 数据卷持久化（建议挂载到 SSD 存储池）
3. 应用日志卷独立管理

```bash
# 在 NAS SSH 环境中
docker compose up -d
docker compose logs -f ffors-api
```

## License

Private — All Rights Reserved.
