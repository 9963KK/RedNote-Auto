# 小红书自动化工具

基于纯国产模型 API 的论文自动化处理与小红书发布工具。

## 功能特性

- 📄 自动抓取 Hugging Face Daily Papers 高赞论文
- 📚 PDF 解析与内容提取（MinerU）
- 🔍 智能识别论文架构图（GLM‑4V + DeepSeek‑OCR）
- 🌐 中英翻译与摘要生成（DeepSeek‑V3/R1）
- ✨ 小红书风格文案润色
- 🚀 自动发布到小红书（xiaohongshu‑mcp‑server）

## 技术栈

- **语言**: Python 3.10+
- **PDF 解析**: MinerU, PyMuPDF
- **OCR**: DeepSeek‑OCR
- **多模态理解**: GLM‑4V
- **文本处理**: DeepSeek‑V3/R1
- **发布**: xiaohongshu‑mcp‑server
- **任务调度**: Celery + Redis (可选)

## 快速开始

### 环境准备

```bash
# 克隆项目
git clone <repository-url>
cd RedNote-Auto

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 配置设置

1. 复制配置文件模板：
```bash
cp config/config.example.yaml config/config.yaml
```

2. 编辑 `config/config.yaml`，填入 API 密钥：
```yaml
apis:
  deepseek:
    api_key: "your_deepseek_api_key"
    base_url: "https://api.deepseek.com"
  glm:
    api_key: "your_glm_api_key"
    base_url: "https://open.bigmodel.cn"
  xiaohongshu_mcp:
    server_url: "http://localhost:3000"
```

### 运行

```bash
# 单次运行
python -m rednote_auto.main --pdf-path /path/to/paper.pdf

# 定时任务模式
celery -A rednote_auto worker -l info
celery -A rednote_auto beat -l info
```

## 项目结构

```
RedNote-Auto/
├── rednote_auto/
│   ├── __init__.py
│   ├── main.py              # 主入口
│   ├── config/              # 配置管理
│   ├── core/                # 核心模块
│   │   ├── pdf_parser.py     # PDF 解析
│   │   ├── ocr_client.py     # OCR 客户端
│   │   ├── vision_client.py   # 视觉理解
│   │   ├── translation_client.py  # 翻译摘要
│   │   └── publisher.py      # 发布模块
│   ├── utils/               # 工具函数
│   └── tests/               # 测试文件
├── config/
│   ├── config.example.yaml
│   └── config.yaml
├── requirements.txt
├── docker-compose.yml        # DeepSeek‑OCR 服务
└── README.md
```

## 开发指南

### 添加新的 API 客户端

1. 在 `rednote_auto/core/` 下创建新模块
2. 继承 `BaseClient` 类
3. 实现 `async process()` 方法
4. 在配置文件中添加相应配置项

### 测试

```bash
# 运行所有测试
pytest

# 运行特定模块测试
pytest tests/test_pdf_parser.py
```

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！