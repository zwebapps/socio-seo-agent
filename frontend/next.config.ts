import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output keeps the production image small: the builder stage emits
  // a self-contained server bundle and the runner copies only that.
  output: "standalone",
  reactStrictMode: true,
};

export default nextConfig;
