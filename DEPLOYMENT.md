# VPS-ONE 部署说明

> 当前版本已移除 EdgeKey 与内置 HashPay 容器，支付直接连接外部 HashPay。后台“站点公开地址”保存后覆盖环境变量 `BASE_URL`，用于生成支付回调和返回地址。

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
- `BASE_URL`：仅首次安装回退地址；后台保存“站点公开地址”后由数据库配置全局覆盖。
- `VPS_ONE_PORT`：主机监听端口，默认 9080。

## CLICD 配置

在 CLICD“API 集成”创建 API Key，并授予 dashboard、container、image、routing、task、security 与 audit 所需权限。后台填写 CLICD 根地址和 API Key，系统统一访问 `/api/v1`；“产品控制”读取容器、主机、路由、任务与安全数据，并支持电源、密码、资源、流量和受确认保护的删除操作。

创建套餐时，表单直接读取 CLICD `/images/enabled` 返回的已启用且已下载镜像，保存时再次远程校验必要字段，不会提前创建真实容器。用户完成 HashPay 支付并通过回调验证后，系统才调用 CLICD 创建对应 VPS。

### 创建容器返回 400

创建载荷严格按 v1 契约发送 `template_id`、资源、网络、SSH 模式、空密码/公钥字段及 `YYYY-MM-DD HH:MM:SS` 到期时间。月流量不再混入创建接口，应在创建成功后通过 `PUT /containers/{id}/traffic-limit` 设置。任务队列和客户控制台会保留 CLICD 的具体错误正文；重点检查 API Key 创建权限、镜像启用状态、宿主资源以及 NAT/IPv6 地址池。修复配置后重试失败任务，订单与实例唯一约束会阻止重复发货。

## HashPay 支付 API 配置与回调

1. 在外部 HashPay 创建商户，取得 Merchant ID、商户 RSA 私钥与 HashPay RSA 公钥。
2. 在 VPS-ONE“系统配置”填写 HashPay 根地址和以上凭据。
3. 在后台配置站点公开 HTTPS 地址；系统自动生成回调：

```text
https://你的域名/hashpay/callback
```

创建订单调用 HashPay `POST /api/merchant/new`，请求头使用 `X-Merchant-ID` 与 `X-Signature`，签名算法为 RSA-SHA256。HashPay 回调必须携带 `X-HashPay-Merchant`，正文为 `key`、`iv`、`data`、`tag` 组成的加密包：RSA-OAEP-SHA256 解密 AES Key，AES-GCM 解密业务载荷。

VPS-ONE 解密后校验 `merchantNo`、金额和 `paid/success/completed` 状态，并以 `eventId` 唯一索引去重。验证成功只写入一次持久化发货任务，后台任务再调用 CLICD 创建 VPS；任务失败采用指数退避重试，避免回调阻塞及重复发货。HashPay 对 HTTP 200 视为接收成功，非 2xx 应重试；生产环境建议限制 HashPay 来源 IP。

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

部署反向代理后，在管理后台将“站点公开地址”保存为正式 HTTPS 地址，无需修改或重启容器。

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
