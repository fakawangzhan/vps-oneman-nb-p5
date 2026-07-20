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
EOF
  chmod 600 .env
fi
docker compose build --pull
docker compose up -d
printf '等待 VPS-ONE 启动'
i=0
while [ "$i" -lt 90 ]; do
  if curl -fsS "http://127.0.0.1:${VPS_ONE_PORT:-9080}/healthz" >/dev/null 2>&1; then
    echo
    echo "安装完成：http://服务器IP:${VPS_ONE_PORT:-9080}/install"
    echo "首次安装后请在系统配置中填写站点地址、CLICD、HashPay 与 SMTP。"
    exit 0
  fi
  printf '.'; i=$((i+1)); sleep 2
done
echo
docker compose logs --tail=120
exit 1
