/**
 * Tooltip (Phase 2, UI refinement).  Wraps a single trigger; shows on hover
 * AND keyboard focus (a11y), hides on blur/leave/Esc.  Associated via
 * aria-describedby.  Pure CSS positioning — light, no popper dependency.
 */
import { useId, useState } from 'react';

export function Tooltip({ label, children, side = 'top', className = '' }) {
  const [open, setOpen] = useState(false);
  const id = useId();

  const pos = {
    top: 'bottom-full left-1/2 -translate-x-1/2 mb-1.5',
    bottom: 'top-full left-1/2 -translate-x-1/2 mt-1.5',
    left: 'right-full top-1/2 -translate-y-1/2 mr-1.5',
    right: 'left-full top-1/2 -translate-y-1/2 ml-1.5',
  }[side] || 'bottom-full left-1/2 -translate-x-1/2 mb-1.5';

  return (
    <span
      className={`relative inline-flex ${className}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); }}
      aria-describedby={open ? id : undefined}
    >
      {children}
      {open && label && (
        <span
          role="tooltip"
          id={id}
          className={`absolute z-50 ${pos} px-2 py-1 rounded-md bg-slate-900 text-white
                      text-xs font-medium whitespace-nowrap shadow-overlay pointer-events-none`}
        >
          {label}
        </span>
      )}
    </span>
  );
}
