# RedNote-Auto 端点重试机制与模型监控规划方案

> **创建日期**: 2025-12-29
> **版本**: v1.0
> **状态**: 待实施

---

## 📋 需求背景

### 当前问题

1. **流程脆弱性**：5 步流水线 (`fetch_papers` → `openai_analyze` → `render_images` → `build_payload` → `publish_mcp`) 中任一节点失败，整个任务需要从头重跑
2. **无断点续传**：失败后丢失已完成步骤的结果，浪费计算资源和 API 调用配额
3. **无模型监控**：配置的多个 API Key 没有可用性检测，故障发现滞后

### 目标

1. **端点重试机制**：支持从失败节点手动/自动重试，保留已成功步骤的结果
2. **模型可用性监控**：前端实时监控所有配置的大模型 API 的健康状态

---

## 🏗️ 架构设计

### 一、端点重试机制

#### 1.1 核心设计思路

```
┌─────────────────────────────────────────────────────────────────┐
│                        任务执行流程                               │
├─────────────────────────────────────────────────────────────────┤
│  fetch_papers → openai_analyze → render_images → build_payload → publish_mcp │
│       ↓              ↓               ↓              ↓              ↓        │
│   [checkpoint]   [checkpoint]    [checkpoint]   [checkpoint]   [checkpoint] │
│                                      ✗                                      │
│                                   失败点                                     │
│                                      ↓                                      │
│                              保存中间状态                                    │
│                                      ↓                                      │
│                         用户点击"从此处重试"                                │
│                                      ↓                                      │
│                    render_images → build_payload → publish_mcp             │
└─────────────────────────────────────────────────────────────────┘
```

#### 1.2 数据结构扩展

**任务状态扩展** (`data/tasks.json`)：

```json
{
  "task_id": "task_publish_20251229_100000",
  "status": "failed",
  "resumable": true,
  "last_successful_step": "openai_analyze",
  "failed_step": "render_images",
  "checkpoint": {
    "papers_data": [...],
    "analyzed_results": [...],
    "step_outputs": {
      "fetch_papers": { "papers": [...], "timestamp": "..." },
      "openai_analyze": { "results": [...], "timestamp": "..." }
    }
  },
  "retry_count": 0,
  "max_retries": 3,
  "steps": [...]
}
```

#### 1.3 后端 API 扩展

| 端点 | 方法 | 描述 |
|------|------|------|
| `/api/tasks/{task_id}/retry` | POST | 从失败步骤重试任务 |
| `/api/tasks/{task_id}/retry/{step_name}` | POST | 从指定步骤重试任务 |
| `/api/tasks/{task_id}/checkpoint` | GET | 获取任务检查点数据 |

#### 1.4 关键实现模块

**文件：`checkpoint_manager.py`** (新建)

```python
class CheckpointManager:
    """任务检查点管理器"""

    def save_checkpoint(self, task_id: str, step_name: str, data: dict)
    def load_checkpoint(self, task_id: str) -> dict
    def get_resumable_step(self, task_id: str) -> str
    def clear_checkpoint(self, task_id: str)
```

**文件：`app.py`** (修改)

- 新增 `retry_task()` 端点处理函数
- 修改 `run_task_with_id()` 支持从中间步骤恢复

**文件：`main.py`** (修改)

- 各步骤函数增加检查点保存逻辑
- 新增 `resume_from_step()` 函数

---

### 二、模型可用性实时监控

#### 2.1 监控架构

```
┌──────────────────────────────────────────────────────────────┐
│                      前端监控面板                              │
├──────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │  OpenAI     │  │  DeepSeek   │  │  QwenVL     │          │
│  │  ● 正常     │  │  ○ 异常     │  │  ● 正常     │          │
│  │  延迟: 1.2s │  │  延迟: --   │  │  延迟: 0.8s │          │
│  │  配额: 80%  │  │  错误: 401  │  │  配额: 95%  │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
│                                                              │
│  最后检查: 2025-12-29 10:30:00  [立即检查] [自动刷新: 60s]   │
└──────────────────────────────────────────────────────────────┘
```

#### 2.2 后端健康检查 API

**文件：`model_health.py`** (新建)

```python
class ModelHealthChecker:
    """模型健康检查器"""

    async def check_openai(self) -> HealthStatus
    async def check_deepseek(self) -> HealthStatus
    async def check_all(self) -> Dict[str, HealthStatus]

@dataclass
class HealthStatus:
    name: str
    status: Literal["healthy", "unhealthy", "unknown"]
    latency_ms: Optional[float]
    error: Optional[str]
    last_check: datetime
    quota_remaining: Optional[float]  # 如果 API 支持
```

**健康检查方式**：
- 发送轻量级测试请求 (如 `models/list` 或简短 completion)
- 超时时间：5 秒
- 检查频率：默认 60 秒，可配置

#### 2.3 API 端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/api/models/health` | GET | 获取所有模型健康状态 |
| `/api/models/health/{model_name}` | GET | 获取指定模型健康状态 |
| `/api/models/health/check` | POST | 立即触发健康检查 |
| `/api/models/config` | GET | 获取模型配置列表 |

#### 2.4 前端实现

**文件：`static/monitor.html`** (修改)

新增模型监控面板组件：
- 模型状态卡片（健康/异常/未知）
- 延迟指示器
- 错误详情展示
- 手动检查按钮
- 自动刷新开关

---

## 📁 文件变更清单

### 新建文件

| 文件路径 | 描述 |
|----------|------|
| `checkpoint_manager.py` | 检查点管理器 |
| `model_health.py` | 模型健康检查模块 |

### 修改文件

| 文件路径 | 变更内容 |
|----------|----------|
| `app.py` | 新增重试 API、健康检查 API |
| `main.py` | 各步骤增加检查点保存、支持断点恢复 |
| `static/monitor.html` | 新增模型监控面板、重试按钮 |
| `openai_config.env` | 新增监控相关配置项 |

---

## ⚙️ 配置扩展

```env
# 新增配置项 (openai_config.env)

# ========== 模型健康检查配置 ==========
MODEL_HEALTH_CHECK_INTERVAL=60      # 健康检查间隔（秒）
MODEL_HEALTH_CHECK_TIMEOUT=5        # 单次检查超时（秒）

# ========== 检查点配置 ==========
CHECKPOINT_RETENTION_HOURS=24       # 检查点数据保留时间（小时）

# ========== 自动重试配置（已启用）==========
AUTO_RETRY_ENABLED=true             # 启用自动重试
AUTO_RETRY_MAX_ATTEMPTS=3           # 最大自动重试次数
AUTO_RETRY_DELAY_SECONDS=30         # 重试间隔（秒）
```

### 用户确认的配置选择

| 配置项 | 用户选择 | 说明 |
|--------|----------|------|
| 重试方式 | **自动+手动** | 失败后自动重试最多3次，同时支持手动触发 |
| 健康检查频率 | **60秒** | 平衡实时性和 API 消耗 |
| 检查点保留 | **24小时** | 保留一天内的检查点数据 |

---

## 🔄 实施计划

### Phase 1: 检查点机制 (核心)

| 步骤 | 任务 | 详情 |
|------|------|------|
| 1 | 创建 `checkpoint_manager.py` | 实现检查点数据的保存/加载、JSON 文件持久化 |
| 2 | 修改 `main.py` | 每个步骤完成后保存检查点、实现 `resume_from_step()` 函数 |
| 3 | 修改 `app.py` | 新增 `/api/tasks/{task_id}/retry` 端点、修改任务执行逻辑支持断点恢复 |

### Phase 2: 模型健康监控

| 步骤 | 任务 | 详情 |
|------|------|------|
| 4 | 创建 `model_health.py` | 实现各模型的健康检查逻辑、异步并发检查、状态缓存 |
| 5 | 修改 `app.py` | 新增健康检查 API 端点、后台定时检查任务 |
| 6 | 修改 `static/monitor.html` | 新增模型监控面板 UI |

### Phase 3: 前端交互优化

| 步骤 | 任务 | 详情 |
|------|------|------|
| 7 | 修改 `static/monitor.html` | 任务卡片新增"重试"按钮、步骤指示器支持点击重试、重试确认对话框 |

---

## ⚠️ 注意事项

1. **数据一致性**：检查点数据需要与任务状态保持同步
2. **存储空间**：定期清理过期检查点数据
3. **并发安全**：重试时需要加锁防止重复执行
4. **API 限流**：健康检查请求需要考虑 API 速率限制
5. **敏感信息**：健康检查 API 不应暴露完整的 API Key

---

## 📊 预期收益

| 指标 | 当前 | 改进后 |
|------|------|--------|
| 失败恢复时间 | 需完全重跑 (5-10分钟) | 从失败点恢复 (1-2分钟) |
| API 调用浪费 | 失败后全部重复 | 保留已成功的调用结果 |
| 故障发现时间 | 任务失败时才发现 | 实时监控，提前预警 |
| 用户体验 | 被动等待失败 | 主动监控，可控重试 |

---

## 📝 变更日志

| 日期 | 版本 | 描述 |
|------|------|------|
| 2025-12-29 | v1.0 | 初始规划方案 |
