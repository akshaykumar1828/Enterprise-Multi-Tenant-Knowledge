# Built React app served by Caddy, which also forwards /api to the api container.
# Build context: the project root (see compose.yaml).
FROM node:20-alpine AS build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM caddy:2-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /frontend/dist /srv
ENV FRONTEND_DIST=/srv \
    API_UPSTREAM=api:8000 \
    CADDY_LOG_DIR=/data/logs \
    SITE_ADDRESS=:80
EXPOSE 80
