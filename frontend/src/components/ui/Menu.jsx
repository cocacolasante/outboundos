/**
 * Dropdown menu (Phase 2, UI refinement).  A trigger button + an animated
 * popover list.  Opens on click; ArrowDown/Up move; Enter selects; Esc + click
 * outside close.  `role="menu"` / `menuitem`.  Reduced motion via MotionConfig.
 */
import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';

import { popVariants } from '../../utils/motion.js';

/**
 * @param trigger ({ open, props }) => ReactNode  — render the trigger; spread
 *   `props` (onClick + aria) onto your button.  Or pass `label` for a default.
 * @param items   [{ label, onSelect, danger, disabled, icon }]
 * @param align   'left' | 'right'
 */
export function Menu({ trigger, label, items = [], align = 'left', testId = 'menu' }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const rootRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e) => { if (!rootRef.current?.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  const enabledIdx = items.map((it, i) => (it.disabled ? -1 : i)).filter((i) => i >= 0);

  function onTriggerKey(e) {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault(); setOpen(true); setActive(enabledIdx[0] ?? -1);
    }
  }
  function onMenuKey(e) {
    if (e.key === 'Escape') { setOpen(false); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const cur = enabledIdx.indexOf(active);
      const nextPos = e.key === 'ArrowDown'
        ? (cur + 1) % enabledIdx.length
        : (cur - 1 + enabledIdx.length) % enabledIdx.length;
      setActive(enabledIdx[nextPos]);
    } else if (e.key === 'Enter' && active >= 0) {
      e.preventDefault();
      select(items[active]);
    }
  }
  function select(item) {
    if (!item || item.disabled) return;
    setOpen(false);
    item.onSelect?.();
  }

  const triggerProps = {
    onClick: () => setOpen((o) => !o),
    onKeyDown: onTriggerKey,
    'aria-haspopup': 'menu',
    'aria-expanded': open,
    'data-testid': `${testId}-trigger`,
  };

  return (
    <div className="relative inline-flex" ref={rootRef}>
      {trigger ? trigger({ open, props: triggerProps }) : (
        <button
          type="button" {...triggerProps}
          className="inline-flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
        >
          {label} <span aria-hidden="true">▾</span>
        </button>
      )}
      <AnimatePresence>
        {open && (
          <motion.div
            role="menu"
            data-testid={testId}
            variants={popVariants}
            initial="hidden" animate="visible" exit="exit"
            onKeyDown={onMenuKey}
            className={`absolute z-50 mt-1 min-w-[10rem] rounded-card border border-slate-200 bg-white shadow-overlay py-1
              ${align === 'right' ? 'right-0' : 'left-0'} top-full`}
          >
            {items.map((it, i) => (
              <button
                key={i}
                type="button"
                role="menuitem"
                disabled={it.disabled}
                data-testid={`${testId}-item-${i}`}
                onMouseEnter={() => setActive(i)}
                onClick={() => select(it)}
                className={`w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 focus:outline-none
                  disabled:opacity-50 disabled:cursor-not-allowed
                  ${it.danger ? 'text-danger-600' : 'text-slate-700'}
                  ${active === i ? (it.danger ? 'bg-danger-50' : 'bg-slate-100') : ''}`}
              >
                {it.icon}{it.label}
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
