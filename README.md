# Relay · 跨节点传输工作台

自托管的 SSH/rsync 传输管理工具，提供账号权限管理、节点管理、文件/目录传输、预约任务、暂停与重试、进度跟踪及可选内容校验。

单个容器包含静态前端、Nginx、Python API 和任务调度服务。数据直接在节点之间传输；控制容器需要能访问节点 SSH 服务。

## 快速启动

需要 Docker Engine 和 Docker Compose。

```sh
cp .env.example .env
docker compose up -d --build
```

打开 `http://localhost:8080/relay/`，首次使用时创建管理员账号，然后添加自己的节点与凭据。没有预置账号、密码、节点或任务。

默认只监听宿主机的 `127.0.0.1:8080`。远程部署时先配置 HTTPS 反向代理，完成管理员初始化前不要开放公网访问。

## HTTPS 部署

将反向代理指向 `http://127.0.0.1:8080`，完整保留 `/relay/` 路径，并设置：

```env
RELAY_ALLOWED_ORIGIN=https://relay.example.com
RELAY_SECURE_COOKIE=1
RELAY_HTTP_BIND=127.0.0.1
RELAY_HTTP_PORT=8080
```

`relay.example.com` 仅是示例，请替换为自己的域名。不要将 API 的内部端口 `18777` 暴露到公网。修改配置后运行 `docker compose up -d`。

## 节点与安全

- 节点需要 SSH、rsync 及应用所需的受限传输命令/目录权限。远端检查和传输会用到无交互 sudo，具体命令见 `server/relay_transfer.py`；应按需要配置最小权限，不要直接授予通用免密 sudo。
- 支持无口令 OpenSSH 私钥或密码认证。凭据保存在持久化目录的受限文件中，不会返回浏览器；这些文件不做静态加密，应保护宿主机、数据卷与备份。
- 每个传输任务使用短期受限密钥；控制节点和数据节点之间必须具备相应网络可达性。
- 容器以非 root 用户运行，默认 UID/GID 为 `998:998`。若改用宿主机目录挂载，需要确保相同的读写权限。

## 运维与备份

```sh
docker compose ps
docker compose logs --tail=100
docker compose exec relay python /app/container-healthcheck.py
```

数据库、节点凭据与 SSH 主机指纹位于 `relay-state` 命名卷。删除容器不会删除该卷，但 `docker compose down -v` 会删除数据，切勿用于日常升级。

备份前等待所有传输结束，然后：

```sh
docker compose stop relay
docker compose cp relay:/var/lib/relay-api ./relay-api-backup
docker compose start relay
```

备份含敏感凭据，不要提交到 GitHub。升级前同样应等待任务结束并备份，再执行 `docker compose up -d --build`。

健康检查同时检测首页与 API；服务进程异常退出时由 Docker 重启整个容器。仅健康检查失败不会自动触发重启。正常停止会停止调度并等待清理，最长等待 70 秒。运行中的任务不能无损续跑，重启后需手动重试。

## 本地开发与测试

需要 Node.js 22.13+、Python 3.12+ 和 OpenSSH。Python 后端仅使用标准库。

```sh
npm ci
npm run dev
```

在另一终端启动开发 API：

```sh
mkdir -p state
RELAY_DB_PATH="$PWD/state/relay.db" RELAY_ALLOWED_ORIGIN=http://localhost:8080 RELAY_SECURE_COOKIE=0 python3 server/relay_api.py
```

前端开发服务器会将 `/relay/api` 转发到本机 `18777` 端口。默认生产容器仍只有一个服务。

```sh
npm run build
npm run lint
PYTHONPATH=server python3 -B -m unittest discover -s server -p test_relay_api.py
```

## 纯净版范围

本仓库仅含通用源码、单容器配置和测试；不包含生产 IP/域名、个人默认值、服务器专用迁移脚本、数据库、凭据、运行日志、构建产物或原项目提交历史。示例主机名与测试地址不代表实际部署环境。
