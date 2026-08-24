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
