# 小红书自动化工具 (RedNote-Auto)

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/platform-linux-lightgrey.svg" alt="Platform">
</p>

基于纯国产模型 API 的论文自动化处理与小红书发布工具。自动抓取 Hugging Face Daily Papers 高赞论文，进行 PDF 解析、内容分析、翻译生成摘要和小红书风格文案，最终自动发布到小红书平台。

## ✨ 核心功能

- 📄 **自动论文抓取** - 从 Hugging Face Daily Papers 抓取高赞论文
- 🤖 **AI 智能分析** - 使用 OpenAI 兼容 API 分析 PDF 内容，提取摘要和架构图描述
- 🎨 **封面图生成** - 使用 PyMuPDF 自动渲染 PDF 首页作为封面图
- 🌐 **中英翻译润色** - 使用 DeepSeek API 翻译并生成小红书风格文案
- 🏷️ **智能标签生成** - 基于论文内容自动生成小红书话题标签
- 🚀 **自动发布** - 通过 MCP 协议自动发布到小红书平台

## 🎯 高级特性

| 特性 | 说明 |
|------|------|
| **Web 监控界面** | 实时监控任务进度、模型健康状态、定时任务配置 |
| **断点续传** | 支持从失败步骤恢复，避免重复工作 |
| **自动重试** | 失败后自动重试，指数退避延迟 |
| **健康监控** | 实时监控所有 API 模型的可用性 |
| **任务调度** | 支持 Cron 定时任务，每天定时发布 |
| **并发处理** | 多篇论文并发分析，提高处理效率 |
| **取消机制** | 支持取消运行中的任务 |
| **进度跟踪** | 实时显示任务执行进度 |

## 🛠️ 技术栈

| 组件 | 技术 | 用途 |
|--------|------|------|
| **后端框架** | FastAPI + Uvicorn | Web 服务和 API |
| **任务调度** | APScheduler | 定时任务执行 |
| **PDF 处理** | PyMuPDF (fitz) | PDF 渲染和图像提取 |
| **API 客户端** | OpenAI SDK | 兼容 OpenAI 的 API 调用 |
| **协议** | MCP (Model Context Protocol) | 小红书发布集成 |
| **前端** | 原生 JavaScript + TailwindCSS | 监控面板 UI |
| **HTTP 客户端** | httpx, aiohttp, requests | 网络请求 |
| **HTML 解析** | BeautifulSoup4 | 爬虫内容解析 |

## 📊 工作流程

```
┌─────────────────────────────────────────────────────────────────┐
│                    论文处理流水线                          │
├─────────────────────────────────────────────────────────────────┤
│ 1. fetch_papers (抓取论文)                                │
│    ↓ 抓取 Hugging Face Daily Papers 高赞论文               │
│    ↓ 保存到 checkpoint                                     │
│                                                           │
│ 2. openai_analyze (AI 分析)                              │
│    ↓ 使用 OpenAI 兼容 API 分析 PDF                         │
│    ↓ 提取 summary + diagram_description                   │
│    ↓ 保存到 checkpoint                                     │
│                                                           │
│ 3. render_images (生成封面图)                           │
│    ↓ 使用 PyMuPDF 渲染 PDF 首页                           │
│    ↓ 保存到 output_hybrid/{paper_name}/cover_page.png      │
│    ↓ 保存到 checkpoint                                     │
│                                                           │
│ 4. build_payload (生成发布文案)                         │
│    ↓ 使用 DeepSeek API 翻译并生成小红书风格文案            │
│    ↓ 生成 tags (使用 AI 生成话题标签)                     │
│    ↓ 保存到 output_hybrid/post_payload.json                 │
│    ↓ 保存到 checkpoint                                     │
│                                                           │
│ 5. publish_mcp (发布到小红书)                             │
│    ↓ 通过 MCP 协议调用 xiaohongshu-mcp-server             │
│    ↓ publish_content 工具                                 │
│    ↓ 保存到 checkpoint                                     │
└─────────────────────────────────────────────────────────────────┘
```

## 🚀 快速开始

### 环境准备

```bash
# 克隆项目
git clone https://github.com/9963KK/RedNote-Auto.git
cd RedNote-Auto

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
pip install -r requirements-web.txt
```

### 配置设置

1. 复制配置文件模板：
```bash
cp openai_config.env.example openai_config.env
```

2. 编辑 `openai_config.env`，填入 API 密钥：

```bash
# OpenAI / 兼容 API
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-5.2
OPENAI_CONCURRENCY=5

# DeepSeek (翻译)
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_CONCURRENCY=5

# QwenVL (视觉)
QWENVL_API_KEY=your_qwenvl_api_key_here
QWENVL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWENVL_MODEL=qwen-vl-plus

# MCP 发布
ENABLE_MCP_PUBLISH=true
XHS_MCP_URL=http://1.13.18.167:18060/mcp

# 监控 / 重试
MODEL_HEALTH_CHECK_INTERVAL=60
CHECKPOINT_RETENTION_HOURS=24
AUTO_RETRY_ENABLED=true
AUTO_RETRY_MAX_ATTEMPTS=3

# 发布控制
PUBLISH_MAX_PAPERS=4
IMAGE_CONCURRENCY=2
```

### 运行

#### 方式一：直接运行 Web 监控服务

```bash
python app.py
```

访问 Web 监控界面：`http://localhost:9999`

#### 方式二：作为 systemd 服务运行

```bash
# 复制服务文件
sudo cp rednote-monitor.service /etc/systemd/system/

# 启动服务
sudo systemctl daemon-reload
sudo systemctl start rednote-monitor
sudo systemctl enable rednote-monitor

# 查看状态
sudo systemctl status rednote-monitor

# 查看日志
sudo journalctl -u rednote-monitor -f
```

## 📱 Web 监控界面

访问 `http://localhost:9999` 可查看监控界面，包含以下功能：

### 任务管理
- 📊 **任务历史** - 查看最近发布任务的执行情况
- ▶️ **手动触发** - 立即触发发布任务
- 🔄 **重试** - 从失败步骤恢复任务
- ❌ **取消** - 取消运行中的任务

### 定时任务
- ⏰ **调度配置** - 设置每天定时发布时间
- 📅 **任务列表** - 查看已配置的调度任务
- ⏭️ **下次运行** - 显示下次执行时间

### 模型监控
- 💚 **健康状态** - 实时显示各模型的健康状态
- ⏱️ **延迟监控** - 显示 API 响应延迟
- 🔍 **错误详情** - 查看失败原因
- 🔄 **手动检查** - 立即触发健康检查

## 🗂️ 项目结构

```
RedNote-Auto/
├── app.py                        # Web 监控服务 (9999 端口)
├── main.py                       # 主工作流 (论文处理流程)
├── crawler.py                    # Hugging Face 论文爬虫
├── mineru_client.py              # MinerU PDF 解析客户端 (备用)
├── openai_client.py              # OpenAI 兼容 API 客户端
├── checkpoint_manager.py         # 检查点管理器
├── model_health.py               # 模型健康检查器
├── pdf_extractor.py              # 本地 PDF 图像提取
├── openai_config.env             # 环境变量配置 (需创建)
├── openai_config.env.example     # 配置示例模板
├── config/
│   └── config.example.yaml       # YAML 配置示例
├── static/
│   └── monitor.html              # 监控面板前端
├── data/
│   ├── tasks.json                # 任务历史
│   ├── scheduler_config.json     # 定时配置
│   ├── checkpoints/              # 检查点数据
│   └── posts.json                # 发布历史
├── output_hybrid/                # 处理输出目录
├── docs/                         # 设计文档
├── logs/                         # 日志目录
├── requirements.txt              # 基础依赖
├── requirements-web.txt          # Web 服务依赖
└── rednote-monitor.service       # systemd 服务配置
```

## 📝 API 端点

| 功能 | 方法 | 端点 | 说明 |
|------|------|------|------|
| **任务管理** | POST | `/api/tasks/trigger` | 手动触发发布任务 |
| | POST | `/api/tasks/{task_id}/retry` | 从失败步骤重试 |
| | POST | `/api/tasks/{task_id}/cancel` | 取消运行中的任务 |
| | GET | `/api/tasks/{task_id}/checkpoint` | 获取检查点数据 |
| **调度管理** | GET | `/api/schedule` | 获取定时配置 |
| | POST | `/api/schedule` | 更新定时配置 |
| | GET | `/api/jobs` | 获取调度任务列表 |
| **模型监控** | GET | `/api/models/health` | 获取所有模型健康状态 |
| | POST | `/api/models/health/check` | 立即触发健康检查 |

## 🔧 开发指南

### 添加新的处理步骤

1. 在 `main.py` 中定义新的步骤函数
2. 在函数中调用 `checkpoint_mgr.save_checkpoint()` 保存结果
3. 在主流程中调用该步骤
4. 在重试逻辑中添加步骤恢复处理

### 添加新的 API 客户端

1. 创建新的客户端类（参考 `openai_client.py`）
2. 实现主要处理方法
3. 在 `model_health.py` 中添加健康检查
4. 在配置文件中添加相应配置项

### 测试

```bash
# 手动触发一次任务
curl -X POST http://localhost:9999/api/tasks/trigger

# 查看任务状态
curl http://localhost:9999/api/tasks

# 检查模型健康
curl http://localhost:9999/api/models/health
```

## 🎨 配置说明

### 调度配置示例

```json
{
  "publish_enabled": true,
  "publish_hour": 9,
  "publish_minute": 30
}
```

### 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENAI_API_KEY` | OpenAI API 密钥 | - |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | - |
| `QWENVL_API_KEY` | QwenVL API 密钥 | - |
| `XHS_MCP_URL` | 小红书 MCP 服务地址 | http://1.13.18.167:18060/mcp |
| `PUBLISH_MAX_PAPERS` | 每次发布的最大论文数 | 4 |
| `AUTO_RETRY_ENABLED` | 是否启用自动重试 | true |
| `MODEL_HEALTH_CHECK_INTERVAL` | 健康检查间隔(秒) | 60 |

## 📊 运行数据

典型执行时间（4 篇论文）：
- 抓取论文: ~1 秒
- OpenAI 分析: ~60 秒
- 渲染封面图: ~5 秒
- 生成发布文案: ~13 秒
- MCP 发布: ~40 秒
- **总计**: ~2 分钟

## 🐛 故障排查

### 任务失败

1. 查看 Web 监控界面的错误信息
2. 检查 `logs/` 目录下的日志文件
3. 查看检查点数据：`data/checkpoints/{task_id}.json`
4. 使用重试功能从失败步骤恢复

### 模型健康检查失败

1. 确认 API 密钥正确
2. 检查 Base URL 是否可访问
3. 查看 `MODEL_HEALTH_CHECK_TIMEOUT` 配置
4. 手动触发健康检查

### MCP 发布失败

1. 确认 `XHS_MCP_URL` 配置正确
2. 检查 MCP 服务是否运行
3. 查看 `data/account.json` 和 `data/auth_cache.json`
4. 参考 [xiaohongshu-mcp-server](https://github.com/benkoo/xiaohongshu-mcp-server) 文档

## 📄 许可证

MIT License

## 🙏 致谢

本项目深受 [xiaohongshu-mcp-server](https://github.com/benkoo/xiaohongshu-mcp-server) 启发，特别感谢该项目为小红书自动化发布提供了优雅的 MCP 协议实现。

感谢以下开源项目：
- [FastAPI](https://fastapi.tiangolo.com/) - 高性能 Web 框架
- [PyMuPDF](https://pymupdf.readthedocs.io/) - PDF 处理库
- [OpenAI Python SDK](https://github.com/openai/openai-python) - OpenAI API 客户端
- [APScheduler](https://apscheduler.readthedocs.io/) - 任务调度库
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) - HTML 解析库

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

### 贡献指南

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'feat: add some amazing feature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 提交 Pull Request

## 📮 联系方式

- GitHub: [@9963KK](https://github.com/9963KK)
- 项目地址: https://github.com/9963KK/RedNote-Auto

---

如果觉得这个项目对你有帮助，请给个 ⭐️ Star 支持一下！
