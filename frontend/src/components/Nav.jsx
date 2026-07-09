import { useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { getMe, logout } from '../api/auth.js';

const ITEMS = [
  {
    to: '/campaigns',
    label: 'Campaigns',
    end: true,
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
      </svg>
    ),
  },
  {
    to: '/leads',
    label: 'Leads',
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
      </svg>
    ),
  },
  {
    to: '/opportunities',
    label: 'Opportunities',
    icon: (
      // Dollar-in-circle — "deals"
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
    ),
  },
  {
    to: '/dashboard',
    label: 'Dashboard',
    icon: (
      // Grid — "dashboard"
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 5h6v6H4V5zm10 0h6v4h-6V5zM4 15h6v4H4v-4zm10-2h6v6h-6v-6z" />
      </svg>
    ),
  },
  {
    to: '/reports',
    label: 'Reports',
    icon: (
      // Bar chart — "reports"
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6m4 6V5m4 14v-9M5 19h14" />
      </svg>
    ),
  },
  {
    to: '/reports/builder',
    label: 'Report builder',
    icon: (
      // Sliders — "build a report"
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h10M4 12h7M4 18h13M16 4v4m-5 2v4m9-8v4" />
      </svg>
    ),
  },
  {
    to: '/replies',
    label: 'Replies',
    icon: (
      // Inbox-arrow-down — "incoming replies"
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
      </svg>
    ),
  },
  {
    to: '/research-client',
    label: 'Research a client',
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
      </svg>
    ),
  },
  {
    to: '/docs',
    label: 'Documentation',
    external: true,
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
      </svg>
    ),
  },
  {
    to: '/settings',
    label: 'Settings',
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
      </svg>
    ),
  },
];

const ADMIN_ITEM = {
  to: '/admin',
  label: 'Admin',
  icon: (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
    </svg>
  ),
};

const STORAGE_KEY = 'ui.nav.collapsed';


function ChevronIcon({ collapsed }) {
  return (
    <svg
      className={`w-4 h-4 transition-transform ${collapsed ? 'rotate-180' : ''}`}
      fill="none" stroke="currentColor" viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
    </svg>
  );
}


export default function Nav() {
  const { data: me } = useQuery({
    queryKey: ['auth-me'],
    queryFn: getMe,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
    } catch {
      /* swallow — non-fatal */
    }
  }, [collapsed]);

  return (
    <nav
      data-testid="nav"
      data-collapsed={collapsed ? 'true' : 'false'}
      className={`${collapsed ? 'w-14 min-w-14' : 'w-60 min-w-60'} bg-slate-900 min-h-screen flex flex-col transition-[width] duration-150`}
    >
      <div className={`flex items-center border-b border-slate-800 ${collapsed ? 'justify-center px-2 py-4' : 'justify-between px-4 py-5'}`}>
        {!collapsed && (
          <div className="flex flex-col">
            <span className="text-white font-bold text-lg tracking-tight leading-tight">OutboundOS</span>
            <span className="text-slate-500 text-[10px] tracking-wide">A CSuite Code Tool</span>
          </div>
        )}
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          className="text-slate-400 hover:text-white hover:bg-slate-800 rounded-md p-1.5 transition-colors"
        >
          <ChevronIcon collapsed={collapsed} />
        </button>
      </div>
      <div className="pt-4 flex-1">
        {!collapsed && (
          <p className="px-6 pb-2 text-xs font-semibold text-slate-500 uppercase tracking-wider">Menu</p>
        )}
        <ul className="list-none p-0 m-0">
          {ITEMS.map((item) => (
            <li key={item.to} className="px-2">
              {item.external ? (
                <a
                  href={item.to}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={collapsed ? item.label : undefined}
                  aria-label={collapsed ? item.label : undefined}
                  className={`flex items-center ${collapsed ? 'justify-center' : 'gap-3'} px-4 py-2.5 rounded-lg text-sm font-medium transition-colors no-underline text-slate-400 hover:text-white hover:bg-slate-800`}
                >
                  {item.icon}
                  {!collapsed && <span>{item.label}</span>}
                </a>
              ) : (
                <NavLink
                  to={item.to}
                  end={item.end}
                  title={collapsed ? item.label : undefined}
                  aria-label={collapsed ? item.label : undefined}
                  className={({ isActive }) =>
                    `flex items-center ${collapsed ? 'justify-center' : 'gap-3'} px-4 py-2.5 rounded-lg text-sm font-medium transition-colors no-underline ${
                      isActive
                        ? 'bg-slate-800 text-white'
                        : 'text-slate-400 hover:text-white hover:bg-slate-800'
                    }`
                  }
                >
                  {item.icon}
                  {!collapsed && <span>{item.label}</span>}
                </NavLink>
              )}
            </li>
          ))}
          {me?.is_superadmin && (
            <li className="px-2 mt-2 pt-2 border-t border-slate-800">
              <NavLink
                to={ADMIN_ITEM.to}
                title={collapsed ? ADMIN_ITEM.label : undefined}
                aria-label={collapsed ? ADMIN_ITEM.label : undefined}
                className={({ isActive }) =>
                  `flex items-center ${collapsed ? 'justify-center' : 'gap-3'} px-4 py-2.5 rounded-lg text-sm font-medium transition-colors no-underline ${
                    isActive
                      ? 'bg-indigo-700 text-white'
                      : 'text-indigo-400 hover:text-white hover:bg-indigo-700/50'
                  }`
                }
              >
                {ADMIN_ITEM.icon}
                {!collapsed && <span>{ADMIN_ITEM.label}</span>}
              </NavLink>
            </li>
          )}
        </ul>
      </div>
      <div className="border-t border-slate-800 px-2 py-3">
        <button
          type="button"
          data-testid="nav-sign-out"
          title={collapsed ? 'Sign out' : undefined}
          aria-label="Sign out"
          onClick={async () => {
            try {
              await logout();
            } catch {
              /* session may already be dead — still leave */
            }
            window.location.assign('/login');
          }}
          className={`w-full flex items-center ${collapsed ? 'justify-center' : 'gap-3'} px-4 py-2.5 rounded-lg text-sm font-medium text-slate-400 hover:text-white hover:bg-slate-800 transition-colors`}
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
          </svg>
          {!collapsed && <span>Sign out</span>}
        </button>
      </div>
    </nav>
  );
}
