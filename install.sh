#!/bin/sh
set -eu
cd "$(dirname "$0")"
command -v docker >/dev/null 2>&1 || { echo '请先安装 Docker 24+'; exit 1; }
docker compose version >/dev/null 2>&1 || { echo '请安装 Docker Compose v2'; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo '请安装 openssl'; exit 1; }
if [ ! -f .env ]; then
  SECRET_KEY=$(openssl rand -hex 32)
  MASTER_KEY=$(openssl rand -hex 32)
  cat > .env <<EOF
SECRET_KEY=$SECRET_KEY
MASTER_KEY=$MASTER_KEY
DATABASE_URL=sqlite+aiosqlite:////app/data/vps-one.sqlite
BASE_URL=http://localhost:9080
DEBUG=false
VPS_ONE_PORT=9080
HASHPAY_PORT=8787
EDGEKEY_PORT=8788
EOF
  chmod 600 .env
fi
if [ ! -f .env.hashpay ]; then
  HASH_SECRET=$(openssl rand -hex 32)
  cat > .env.hashpay <<EOF
TGBOT_TOKEN=
APP_SECRET=$HASH_SECRET
EOF
  chmod 600 .env.hashpay
fi
if [ ! -f .env.edgekey ]; then
  cat > .env.edgekey <<EOF
# 按 EdgeKey 文档补充生产密钥；首次启动后在 http://127.0.0.1:8788 初始化。
EOF
  chmod 600 .env.edgekey
fi
docker compose build --pull
docker compose up -d
printf '等待 VPS-ONE 启动'
i=0
while [ "$i" -lt 90 ]; do
  if curl -fsS "http://127.0.0.1:${VPS_ONE_PORT:-9080}/healthz" >/dev/null 2>&1; then
    echo
    echo "安装完成：http://服务器IP:${VPS_ONE_PORT:-9080}/install"
    echo "HashPay：http://127.0.0.1:${HASHPAY_PORT:-8787}"
    echo "EdgeKey：http://127.0.0.1:${EDGEKEY_PORT:-8788}"
    exit 0
  fi
  printf '.'; i=$((i+1)); sleep 2
done
echo
docker compose logs --tail=120
exit 1
