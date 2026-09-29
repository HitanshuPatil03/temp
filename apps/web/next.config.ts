import path from "node:path";

import type { NextConfig } from "next";

/**
 * The FastAPI backend runs as its own process, and `/api/*` is forwarded to it
 * by `src/proxy.ts` rather than by a rewrite here.
 *
 * That moved when authentication landed: the proxy has to attach the session
 * cookie's token as an `Authorization` header, so it is already deciding what
 * the forwarded request looks like. Splitting the destination (config) from the
 * credential (proxy) across two files would mean two places to change when the
 * backend moves, and one of them would be missed.
 *
 * What still holds either way: the browser never learns the backend's origin, so
 * no request is cross-origin and CORS stays out of the failure surface.
 */
const nextConfig: NextConfig = {
  /**
   * Trace the dependency graph and emit a self-contained server bundle, so the
   * container image carries only the files the server actually loads rather than
   * the whole of `node_modules`.
   */
  output: "standalone",

  /**
   * Pin the Turbopack root to this workspace. Without it, Turbopack walks up
   * looking for a lockfile and finds a stray one in the user's home directory —
   * outside the repository — then warns and ignores it. Pinning makes the build
   * independent of whatever happens to sit above the checkout, which matters more
   * on a build server than on a laptop.
   */
  turbopack: {
    root: path.resolve(import.meta.dirname),
  },
};

export default nextConfig;
