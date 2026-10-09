import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';

export const metadata: Metadata = {
  title: '小红书阅读器',
  description: '从 PostgreSQL 阅读已抓取的小红书笔记',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <header className="topbar">
          <Link href="/" className="brand"><span>◈</span> 小红书阅读器</Link>
          <nav><Link href="/">阅读</Link><Link href="/settings">设置</Link></nav>
        </header>
        <main className="shell">{children}</main>
      </body>
    </html>
  );
}
