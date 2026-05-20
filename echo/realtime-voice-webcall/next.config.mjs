const backendApiBase = process.env.BACKEND_API_BASE || "http://127.0.0.1:8765";

const nextConfig = {
  output: "export",
  images: {
    unoptimized: true,
  },
  async rewrites() {
    if (process.env.NODE_ENV !== "development") {
      return [];
    }

    return [
      { source: "/health", destination: `${backendApiBase}/health` },
      { source: "/instances", destination: `${backendApiBase}/instances` },
      { source: "/session/:path*", destination: `${backendApiBase}/session/:path*` },
      { source: "/auth/:path*", destination: `${backendApiBase}/auth/:path*` },
      { source: "/token", destination: `${backendApiBase}/token` },
      { source: "/orchestrate", destination: `${backendApiBase}/orchestrate` },
      { source: "/dispatch", destination: `${backendApiBase}/dispatch` },
      { source: "/bridge/:path*", destination: `${backendApiBase}/bridge/:path*` },
      { source: "/agent/:path*", destination: `${backendApiBase}/agent/:path*` },
      { source: "/twilio/:path*", destination: `${backendApiBase}/twilio/:path*` },
    ];
  },
};

export default nextConfig;
