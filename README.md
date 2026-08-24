# Customer Service Agent

本项目实现了一个支持物流查询、换货/退款确认、会话状态管理的客服 Agent，并通过 FastAPI 暴露 API。  
V10 引入了 SQLite 持久化：会话状态与售后幂等记录可落库，容器重启和镜像更新后不会清空状态（当 `AGENT_DB_PATH` 配置到可持久化目录时）。

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
