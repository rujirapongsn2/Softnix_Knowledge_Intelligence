FROM node:22-alpine AS build
WORKDIR /app
ARG MAX_FILE_SIZE_MB=100
ENV VITE_MAX_FILE_SIZE_MB=$MAX_FILE_SIZE_MB
COPY package.json .
RUN npm install
COPY . .
RUN npm run build
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
