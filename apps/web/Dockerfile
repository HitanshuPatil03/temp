# MRIP web image.
#
# Built on Next's standalone output, which traces the dependency graph and copies
# only the files the server actually loads — no node_modules in the runtime layer.
#
# Note there is no build-time API URL. The browser never calls the backend
# cross-origin; Next rewrites `/api/*` to `MRIP_API_URL`, which is read at
# **runtime**. So one image serves every environment, and pointing it at a
# different backend is a restart rather than a rebuild.

# ---------------------------------------------------------------- deps stage
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
# `npm ci` when there is a lockfile, `npm install` when there is not, so the
# image builds from a fresh checkout either way.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# --------------------------------------------------------------- build stage
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ------------------------------------------------------------- runtime stage
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -g 10001 nodejs && adduser -u 10001 -G nodejs -S nextjs

COPY --from=build /app/public ./public
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
