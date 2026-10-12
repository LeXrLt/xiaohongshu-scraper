import type { Metadata } from 'next';
import Link from 'next/link';
import './globals.css';
import SidebarNav from './sidebar-nav';

export const metadata: Metadata = {
  title: '小红书阅读器',
  description: '从 PostgreSQL 阅读已抓取的小红书笔记',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <div className="app-frame">
          <aside className="sidebar">
            <Link href="/" className="brand"><span>◈</span><b>小红书阅读器</b></Link>
            <SidebarNav />
            <div className="sidebar-footer">PERSONAL READER<br /><span>LOCAL INDEX</span></div>
          </aside>
          <div className="content-frame"><main className="shell">{children}</main></div>
        </div>
      </body>
    </html>
  );
}
