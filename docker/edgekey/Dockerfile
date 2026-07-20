FROM oven/bun:1-alpine
RUN apk add --no-cache git
WORKDIR /app
RUN git clone --depth 1 https://gh-proxy.com/34892002/edgeKey.git . && bun install --frozen-lockfile && bun run build
EXPOSE 8788
CMD ["sh","-c","bunx wrangler d1 migrations apply edgekey-db --local --persist-to /data && bunx wrangler dev --local --ip 0.0.0.0 --port 8788 --persist-to /data"]
