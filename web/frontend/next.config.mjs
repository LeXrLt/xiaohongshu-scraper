import nextEnv from '@next/env';
import { fileURLToPath } from 'node:url';

// Local development and Python share the repository-root .env.
nextEnv.loadEnvConfig(fileURLToPath(new URL('../../', import.meta.url)));

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  env: {
    // Browser-side exchange requires a public key; see the deployment note in README.
    NEXT_PUBLIC_API_AUTH_KEY: (process.env.API_AUTH_KEY || '').trim(),
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${process.env.BACKEND_URL || 'http://localhost:8000'}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
