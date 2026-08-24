# Customer Service Agent

本项目实现了一个支持物流查询、换货/退款确认、会话状态管理的客服 Agent，并通过 FastAPI 暴露 API。  
当前版本使用**内存会话**，仅适合单进程演示环境（`InMemorySessionStore` 不支持跨进程共享）。

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

- 会话状态仅保存在进程内存中。
- InMemorySessionStore 不支持多进程共享，因此服务必须使用单 worker（`--workers 1`）运行。
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

默认仅监听本机回环地址，本地安全；若要开放局域网访问，需要显式设置 `BIND_ADDRESS` 为网卡 IP 或 `0.0.0.0`。

注意：

- 容器为单进程内存会话模式，重启后会话状态会丢失。
- 如果镜像来自私有 GHCR，需先执行登录：

```bash
docker login ghcr.io
```

- 不要把 GHCR token 写入 `.env`。
