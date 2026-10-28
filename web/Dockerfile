# syntax=docker/dockerfile:1
# ---- build: install exact deps, typecheck + bundle ---------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---- serve: static files + /api reverse proxy (SSE-friendly) -----------------------------------
# nginx stable branch. `apk upgrade` takes Alpine fixes released since the base image was built: the Grype gate in
# supply-chain.yml fails on any fixable high/critical package.
FROM nginx:1.30-alpine
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1/healthz || exit 1
