# Build the dashboard with Node 22 / npm 11 (npm 10 crashes on this tree), then
# serve the static files from nginx, which also proxies /api and /ws to the backend.
FROM node:22.23.4-alpine AS build
WORKDIR /src
COPY package.json package-lock.json .npmrc ./
RUN npx -y npm@11.21.1 ci
COPY . .
RUN npm run build

FROM nginx:1.30.6-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO /dev/null http://127.0.0.1/ || exit 1
