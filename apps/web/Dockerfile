FROM node:22-alpine AS build
WORKDIR /app
ARG MAX_FILE_SIZE_MB=100
ENV VITE_MAX_FILE_SIZE_MB=$MAX_FILE_SIZE_MB
COPY package.json .
RUN npm install
COPY . .
RUN npm run build
FROM nginx:alpine
# MAX_FILE_SIZE_MB is read again at container start (config.js and nginx client_max_body_size); the build-arg
# above is only the fallback baked into the bundle.
ARG MAX_FILE_SIZE_MB=100
ENV MAX_FILE_SIZE_MB=$MAX_FILE_SIZE_MB
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --chmod=755 docker/40-ski-config.sh /docker-entrypoint.d/40-ski-config.sh
