FROM node:22-alpine
RUN apk add --no-cache git
WORKDIR /app
RUN git clone --depth 1 https://github.com/TGDash/HashPay.git . && npm ci && npm run build
EXPOSE 8787
CMD ["sh","-c","npm run db:migrate:local && npx wrangler dev --local --ip 0.0.0.0 --port 8787 --persist-to /data"]
