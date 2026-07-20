# VPS-ONE 部署说明

## 系统架构

VPS-ONE 使用 Python 3.12、FastAPI、SQLAlchemy async、aiosqlite、Jinja2。SQLite 启用 WAL、NORMAL 同步、15 秒 busy timeout，并通过短事务与应用写锁优化单机并发。系统不支持多容器同时写同一 SQLite 文件。

## Docker 一键安装

要求：Linux、Docker 24+、Docker Compose v2、`curl`、`openssl`，服务器开放 8080 端口。

```bash
chmod +x install.sh
./install.sh
```

脚本自动生成 `.env` 密钥、构建镜像、启动服务并等待健康检查。完成后访问：

```text
http://服务器IP:9080/install
```

创建管理员后安装入口自动锁定。后台地址为 `/admin`，接口配置为 `/admin/settings`。

## 环境变量

- `SECRET_KEY`：会话及 CSRF 签名密钥，禁止修改，否则现有会话失效。
- `MASTER_KEY`：API 凭据加密密钥，禁止丢失或修改。
- `DATABASE_URL`：默认 `sqlite+aiosqlite:////app/data/vps-one.sqlite`。
- `BASE_URL`：站点公开 HTTPS 地址，用于支付返回地址。
- `VPS_ONE_PORT`：主机监听端口，默认 9080。

## CLICD 配置

后台填入 CLICD Base URL 与 Bearer Token。系统统一访问 `/api/v1`，套餐映射 CPU、内存、磁盘、流量、带宽、节点、镜像及到期时间；支持创建、状态读取、开关机、重启、重装、重置密码、流量限制与续期。不同 CLICD 发行版字段可能不同，生产上线前请用测试节点验证。

## HashPay 配置

后台填入 HashPay 地址、Merchant ID、商户 RSA 私钥及 HashPay 公钥。支付回调：

```text
https://你的域名/hashpay/callback
```

请求采用 RSA-SHA256 签名；加密回调采用 RSA-OAEP-SHA256 解密 AES Key，再以 AES-GCM 解密载荷。系统复核订单号、金额、状态并以事件唯一键保证回调幂等。

## Nginx HTTPS 反向代理

```nginx
server {
    listen 443 ssl http2;
    server_name vps.example.com;
    client_max_body_size 2m;
    location / {
        proxy_pass http://127.0.0.1:9080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP $remote_addr;
    }
    location /static/ {
        proxy_pass http://127.0.0.1:9080;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

将 `.env` 中 `BASE_URL` 改为正式 HTTPS 地址后执行 `docker compose up -d`。

## 备份与恢复

在线一致性备份：

```bash
docker compose exec vps-one python -c "import sqlite3; a=sqlite3.connect('/app/data/vps-one.sqlite'); b=sqlite3.connect('/app/data/backup.sqlite'); a.backup(b)"
docker cp "$(docker compose ps -q vps-one):/app/data/backup.sqlite" ./vps-one-backup.sqlite
```

恢复前停止服务，备份当前数据卷，再替换数据库文件。`.env` 必须与数据库同时备份，否则加密凭据无法解密。

## 更新与排错

```bash
docker compose build --no-cache
docker compose up -d
docker compose logs -f --tail=200 vps-one
curl -fsS http://127.0.0.1:9080/healthz
```

SQLite 报只读时检查数据卷权限；HashPay 失败时检查系统时间、私钥格式和公开回调地址；CLICD 失败时检查 Bearer Token、节点名和镜像标识。生产建议限制后台访问来源并开启 HTTPS。
