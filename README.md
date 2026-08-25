# Customer Service Agent

本项目实现了一个支持物流查询、换货/退款确认、会话状态管理的客服 Agent，并通过 FastAPI 暴露 API。  
V10 引入了 SQLite 持久化：会话状态与售后幂等记录可落库，容器重启和镜像更新后不会清空状态（当 `AGENT_DB_PATH` 配置到可持久化目录时）。
V11 增加了“确定性对话评测”与“脱敏运行追踪”：请求会经过 tracing 管道记录每一轮工具/状态迁移，且仅输出结构化日志，不落库。

## V14A 订单查询 Provider

订单查询默认继续使用 V0–V13B 的进程内 `order_lookup`。只有显式设置
`AGENT_ORDER_PROVIDER=http` 才会创建只读 HTTP Provider；换货、退款创建仍然使用
现有的内存/SQLite 确定性服务，不会被远程写入替代。

HTTP Provider 使用固定的 `GET {AGENT_ORDER_API_BASE_URL}/orders/{order_id}`，带有
`Authorization: Bearer ...` 和 `Accept: application/json`。订单号仍必须是项目现有的
单字母加四位数字格式，用户输入不能改变目标 URL。生产上游必须使用 HTTPS；HTTP 仅允许
`localhost` 或 `127.0.0.1`，用于本地测试。API token 只通过运行时环境变量注入，不写入
镜像、日志、trace 或响应。

可配置项：

- `AGENT_ORDER_API_BASE_URL`、`AGENT_ORDER_API_TOKEN`：HTTP 模式必填；URL 不得带用户信息、query 或 fragment。
- `AGENT_ORDER_CONNECT_TIMEOUT_SECONDS`、`AGENT_ORDER_READ_TIMEOUT_SECONDS`：连接和读取超时。
- `AGENT_ORDER_MAX_ATTEMPTS`：总尝试次数，严格限制为 1–3；只对临时网络错误、408、429 和 5xx 查询重试，并使用有上限的指数退避。
- `AGENT_ORDER_CIRCUIT_FAILURE_THRESHOLD`、`AGENT_ORDER_CIRCUIT_COOLDOWN_SECONDS`：熔断阈值和冷却时间。

熔断器状态为 `CLOSED`、`OPEN`、`HALF_OPEN`：达到临时失败阈值后快速失败，冷却后只放行一个探测请求，探测成功恢复，失败重新打开。熔断状态只存在于单个进程/Provider 实例内，不跨 worker、容器或副本共享。`/health` 不访问订单上游。

HTTP 200 映射为现有 `OrderRecord`，404 映射为未找到；400、401、403 和非法上游响应不重试，分别作为安全的上游错误返回。临时不可用返回安全 503，协议错误返回安全 502；客户端不会看到上游正文或内部 URL。查询失败时会话状态不会保存变化。

## V10 持久化说明

会话与换货/退款申请在以下条件下持久化：

- 设置环境变量 `AGENT_DB_PATH`（例如 `/data/customer-service-agent.db`）时启用 SQLite 持久化。
- 仍保留原有内存实现用于 CLI、单元测试与向后兼容；当未设置 `AGENT_DB_PATH` 时继续走内存模式。
- 部署默认使用命名卷挂载到容器内 `/data`，并持久化 `customer-service-agent.db`。

约束说明：

- `AGENT_DB_PATH` 方案仅适合**单服务器、单实例、单 worker**。
- 容器普通重启或更新不会清理数据。
- `docker compose down -v` 会删除命名卷及数据库文件，请谨慎使用。
- 首次部署 V10 时，旧版内存会话不能自动迁移到新数据库；这次升级会导致历史会话在新实例中不可恢复。

示例 `.env` 不应提交，本文不会展示真实 `.env` 内容，仅说明所需字段：

- `AGENT_DB_PATH=/data/customer-service-agent.db`

## 本地开发与测试

```bash
python -m pytest -q
```

## API 鉴权与安全访问

本地开发默认 `AGENT_AUTH_REQUIRED=false`，保持旧的 CLI、测试和本地调用方式。生产环境应启用鉴权，并使用至少 32 个字符的随机 Key：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

生产环境配置 `AGENT_AUTH_REQUIRED=true` 和 `AGENT_API_KEY`。当鉴权开启时，Key 缺失、错误或配置为占位符都会导致启动失败或返回通用 `401 Unauthorized`，不会静默降级。`/health` 始终公开；创建、读取会话和发送消息都需要 `X-API-Key`：

```bash
curl -H 'X-API-Key: YOUR_API_KEY' -X POST http://localhost:8000/sessions
curl -H 'X-API-Key: YOUR_API_KEY' http://localhost:8000/sessions/SESSION_ID
curl -H 'Content-Type: application/json' -H 'X-API-Key: YOUR_API_KEY' \
  -d '{"message":"我要投诉"}' \
  -X POST http://localhost:8000/sessions/SESSION_ID/messages
```

在笔记本访问台式机服务前，必须先启用 API Key 鉴权。API Key 在普通 HTTP 中不会加密；非可信网络应使用 TLS、反向代理或加密隧道。不要提交真实密钥。CORS 只限制浏览器跨源访问，不是安全认证机制。

可用 `AGENT_DOCS_ENABLED` 显式控制 `/docs`、`/redoc` 和 `/openapi.json`；鉴权生产模式默认关闭。`AGENT_CORS_ORIGINS` 使用逗号分隔的精确 origin，未配置时不启用 CORS。

## V13B 知识库政策问答

请求分为两类：`action` 是执行物流查询、换货、退款或投诉流程；`information` 是咨询政策、期限、条件或状态含义。信息咨询可以检索知识库并由模型组织答案，但不会参与资格判断、写操作授权或人工确认边界。

知识回答必须携带本次检索结果中的有效引用，API 会拒绝空引用、重复引用、未知引用或空回答。知识库没有足够依据时，系统直接拒答：`当前知识库中没有找到足够依据，请换一种方式描述或联系人工客服。` RAG 不会创建换货/退款申请，也不会改变确定性业务规则。

## 对话评测与单元测试的区别

- 单元测试（`pytest`）验证业务函数、状态机分支和并发保存路径。
- 评测（`python -m evals.run`）按固定场景驱动完整会话流程，验证“真实流程走法”是否稳定一致，包括工具调用次数和终态一致性。

## 运行评测

```bash
python -m evals.run
```

可选输出报告：

```bash
python -m evals.run --output eval-reports/latest.json
```

## Trace 脱敏边界

- 不会记录原始用户消息。
- 不会记录订单号、申请原因、`request_id`、环境变量或模型完整响应。
- 会记录已脱敏的状态快照（`intent`、`status`、是否存在订单/售后单）以及调用摘要（调用类型、结果、耗时）。
- 当前 Trace 仅通过日志单行 JSON 输出，不持久化到 SQLite 或文件。

推荐本地依赖安装：

```bash
python -m pip install -r requirements-dev.txt
```

## 本地镜像构建与运行

```bash
docker build -t customer-service-agent:local .
docker run --rm -p 8000:8000 --env-file .env customer-service-agent:local
```

访问健康检查：

```bash
curl http://localhost:8000/health
```

预期返回：

```json
{"status":"ok"}
```

说明：容器运行时的密钥与配置通过环境变量（如 `LLM_API_KEY`）注入，不会在镜像构建中写入 `.env` 或任何密钥。

## GHCR 镜像发布与拉取

镜像发布仓库：`ghcr.io/<owner>/<repo>`

示例拉取命令：

```bash
docker pull ghcr.io/<owner>/<repo>:latest
docker pull ghcr.io/<owner>/<repo>:<tag>
```

## CI/CD 说明

- PR：自动运行测试，并验证 Docker 镜像可构建，但不推送。
- `main` 分支推送与 `v*` 标签推送：测试通过后构建并推送镜像到 GHCR。
- 目前仅提供 **Continuous Delivery 到镜像仓库**，不包含自动部署到具体服务器。
- CI 显式设置 `AGENT_ORDER_PROVIDER=memory`，测试与镜像 smoke test 不使用真实订单 token，也不访问外部订单系统。

## 会话与安全边界

- V10 默认通过 SQLite 持久化会话（当设置 `AGENT_DB_PATH`）或继续使用内存会话（未设置时）。
- 无论持久化或内存模式，本服务均为单进程、单实例、单 worker 演示定位，不保证跨容器/多实例共享。
- 任何 `.env` 文件与密钥不会打进镜像或 GitHub Actions 构建参数中。

## 双开发机场景部署（台式机服务目录）

目标是让“源码开发目录”和“服务运行目录”分离：

- 笔记本/台式机开发目录：`~/customer-service-agent`
- 台式机服务目录：`~/services/customer-service-agent`

建议流程：

1. 在源码目录准备部署文件（只需在服务目录保留这三类文件）：

```bash
mkdir -p ~/services/customer-service-agent
cp deploy/compose.yaml deploy/deploy.sh deploy/.env.example ~/services/customer-service-agent/
chmod +x ~/services/customer-service-agent/deploy.sh
```

2. 在服务目录创建真实环境文件并保护权限：

```bash
cd ~/services/customer-service-agent
cp .env.example .env
chmod 600 .env
```

3. 启动服务：

```bash
./deploy.sh
```

更新服务版本时直接再次运行：

```bash
./deploy.sh
```

回滚时可修改 `IMAGE_TAG` 指向可用版本或 commit SHA（例如 `sha-...`）后再执行 `./deploy.sh`。

示例 `.env` 关键项：

- `IMAGE_TAG`：默认 `latest`，也可改为镜像 tag（如 `v1.2.3`、`sha-xxxxx`）
- `BIND_ADDRESS`：默认 `127.0.0.1`，仅本机可访问；如需局域网访问改为 `0.0.0.0`
- `HOST_PORT`：对外端口
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
- `AGENT_DB_PATH`：SQLite 持久化文件路径，示例 `/data/customer-service-agent.db`

默认仅监听本机回环地址，本地安全；若要开放局域网访问，需要显式设置 `BIND_ADDRESS` 为网卡 IP 或 `0.0.0.0`。

注意：

- V10 下，若使用 SQLite 持久化，容器更新不会清理 `customer-service-agent.db`；若环境未设置 `AGENT_DB_PATH`，行为仍为内存模式，重启会丢失会话状态。
- 如果镜像来自私有 GHCR，需先执行登录：

```bash
docker login ghcr.io
```

- 不要把 GHCR token 写入 `.env`。
