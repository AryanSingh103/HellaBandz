/** @type {import('next').NextConfig} */
const nextConfig = { reactStrictMode: true, output: "standalone", distDir: process.env.QS_WEB_DIST_DIR || ".next" };
export default nextConfig;
