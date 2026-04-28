# API Router

负载均衡反向代理，将请求均匀分发到同一模型的多个部署实例上。

## 功能

- **自动负载均衡** — 基于实时 in-flight 请求数，选择负载最低的后端；负载相同时随机选择
- **健康检查** — 定期探测各后端，连续 5 次失败自动摘除，恢复后自动加回
- **高并发** — 全异步架构（async + aiohttp），支持大量并发请求
- **请求重试** — 后端返回 5xx 时自动切换到下一个可用后端重试
- **路由日志** — 每笔请求记录：时间、路由目标、响应时间（JSON 格式）
- **TUI 仪表盘** — 实时展示各后端状态、负载、响应时间、错误信息

## 项目结构

```
api-router/
├── config.yaml      # 配置文件
├── main.py          # 启动入口
├── router.py        # 核心路由：LoadBalancer + HealthChecker + APIRouter
├── proxy.py         # aiohttp 本地代理服务
├── tui.py           # Textual TUI 仪表盘
├── models.py        # Backend 数据模型
├── logger.py        # 日志模块
└── requirements.txt # Python 依赖
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 编辑配置

打开 `config.yaml`，修改以下内容：

```yaml
# 你的共享 API Key
api_key: sk-xxxxxxxxxxxxxxxx

# 4 个后端部署地址
backends:
  - name: api-1
    url: https://api1.example.com
  - name: api-2
    url: https://api2.example.com
  - name: api-3
    url: https://api3.example.com
  - name: api-4
    url: https://api4.example.com
```

### 3. 启动

**带 TUI 仪表盘（默认）：**

```bash
python main.py
```

**无 TUI（无头模式 / 后台运行）：**

```bash
python main.py --no-tui
```

**自定义配置路径：**

```bash
python main.py -c /path/to/config.yaml
```

**调试：覆盖超时时间（秒）：**

```bash
python main.py --timeout 30
```

### 4. 使用

启动后，API Router 在 `http://localhost:8600` 监听，使用方式与原始 API 完全一致：

```bash
# 发送请求（和直连后端一样，只是地址换成 router）
curl http://localhost:8600/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"hello"}]}'

# 查看路由状态
curl http://localhost:8600/router/stats

# 查看代理健康状态
curl http://localhost:8600/router/health
```

## TUI 仪表盘说明

启动后会显示实时监控界面：

| 区域       | 说明                                       |
| ---------- | ------------------------------------------ |
| Stats Bar  | 全局统计：总请求数、成功数、失败数         |
| Backend    | 各后端状态、负载、发送/接收数、平均响应时间 |
| Error Log  | 最近错误信息                               |

快捷键：`r` 手动刷新，`q` 退出。

## 核心调度逻辑

```
请求到达 → 筛选可用后端 → 按 current_load 排序 → 选负载最低的
                                                    ↓ (负载相同)
                                                随机选择
```

- 每发送一个请求：该后端 `current_load += 1`
- 每收到一个响应：该后端 `current_load -= 1`
- 连续 5 次请求失败 → 该后端标记为 `unavailable`，不再参与调度
- 健康检查连续成功 → 标记为 `recovering`，再成功后恢复 `available`

## 日志

日志文件位于 `logs/` 目录：

- `routing.log` — 每条请求的路由详情（JSON 格式）
- `health.log` — 后端健康状态变更记录

`routing.log` 示例：

```json
{"event": "request_start", "request_id": "a1b2c3d4", "timestamp": "2025-01-01T00:00:00+00:00", "method": "POST", "path": "/v1/chat/completions", "backend_name": "api-2", "backend_url": "https://api2.example.com"}
{"event": "request_end",   "request_id": "a1b2c3d4", "timestamp": "2025-01-01T00:00:01+00:00", "backend_name": "api-2", "status_code": 200, "response_time_ms": 850.3, "success": true}
```

## 代理设置

如果后端 API 需要通过代理访问，在 `config.yaml` 中设置：

```yaml
http_proxy: http://127.0.0.1:7890
```

支持 HTTP 和 SOCKS5 代理（如 `socks5://127.0.0.1:1080`）。留空或删除该行则不使用代理。所有后端共享同一个代理地址。


## 配置参数说明

| 参数                         | 默认值 | 说明                               |
| ---------------------------- | ------ | ---------------------------------- |
| `proxy.port`                 | 8600   | 本地监听端口                       |
| `health_check.interval`      | 10     | 健康检查间隔（秒）                 |
| `health_check.failure_threshold` | 5  | 连续失败多少次标记不可用           |
| `health_check.recovery_threshold` | 2 | 恢复时需连续成功多少次             |
| `http_proxy`                | (空)   | 出站代理地址，支持 http/socks5          |
| `request.timeout`            | 120    | 转发请求超时时间（秒），可 CLI 覆盖 |
| `request.max_retries`        | 2      | 失败后最多重试几个不同后端         |
| `tui.refresh_interval`       | 1      | TUI 刷新间隔（秒）                 |
