'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const items = [
  { href: '/', label: '笔记', icon: '阅' },
  { href: '/merchants', label: '商户', icon: '店' },
  { href: '/settings', label: '设置', icon: '设' },
];

export default function SidebarNav() {
  const pathname = usePathname();
  return <nav className="side-nav" aria-label="主导航">
    {items.map((item) => {
      const active = item.href === '/' ? pathname === '/' : pathname.startsWith(item.href);
      return <Link href={item.href} key={item.href} className={active ? 'active' : undefined} aria-current={active ? 'page' : undefined}>
        <span className="nav-icon">{item.icon}</span><span>{item.label}</span>
      </Link>;
    })}
  </nav>;
}
