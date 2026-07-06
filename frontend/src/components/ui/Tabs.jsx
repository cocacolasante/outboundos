/**
 * Accessible tab strip (Phase 2, UI refinement).  Replaces the 3 hand-rolled
 * strips (CampaignDetail / Settings / SocialRadar) + their blue-600/700 drift.
 * Roving tabindex + ArrowLeft/Right/Home/End, role=tablist/tab, aria-selected.
 * The caller renders the active panel; this is just the strip.
 */
import { useRef } from 'react';
import { motion } from 'framer-motion';
import { spring } from '../../utils/motion.js';

export function Tabs({ tabs, active, onChange, className = '', testId = 'tabs' }) {
  const ref = useRef(null);

  function onKeyDown(e) {
    const keys = ['ArrowRight', 'ArrowLeft', 'Home', 'End'];
    if (!keys.includes(e.key)) return;
    e.preventDefault();
    const i = tabs.findIndex((t) => t.key === active);
    let next = i;
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    const nextKey = tabs[next]?.key;
    if (nextKey) {
      onChange?.(nextKey);
      ref.current?.querySelector(`[data-tab="${nextKey}"]`)?.focus();
    }
  }

  return (
    <div
      ref={ref}
      role="tablist"
      data-testid={testId}
      onKeyDown={onKeyDown}
      className={`flex gap-1 border-b border-slate-200 ${className}`}
    >
      {tabs.map((t) => {
        const selected = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            data-tab={t.key}
            data-testid={`${testId}-${t.key}`}
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange?.(t.key)}
            className={`relative px-3.5 py-2 -mb-px text-sm font-medium border-b-2 border-transparent transition-colors duration-fast
              focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded-t
              ${selected
                ? 'text-brand-700'
                : 'text-slate-500 hover:text-slate-800'}`}
          >
            {t.label}
            {t.count != null && (
              <span className={`ml-1.5 text-xs ${selected ? 'text-brand-500' : 'text-slate-400'}`}>
                {t.count}
              </span>
            )}
            {selected && (
              // Shared-layout underline that glides between tabs as the active
              // key changes (snaps under reduced motion via the root MotionConfig).
              <motion.span
                layoutId={`${testId}-indicator`}
                className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-brand-600"
                transition={spring}
              />
            )}
          </button>
        );
      })}
    </div>
  );
}
