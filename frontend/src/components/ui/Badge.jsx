/**
 * Badge / status pill (Phase 2, UI refinement).  Uses the one semantic
 * palette from utils/statusColors.js so every status chip matches.
 */
import { SEMANTIC } from '../../utils/statusColors.js';

const BASE = 'inline-flex items-center gap-1 px-2 py-0.5 rounded-pill text-[11px] font-medium';

/** `variant` is a semantic key: neutral | brand | info | success | warning |
 *  danger | purple.  `dot` adds a leading status dot. */
export function Badge({ variant = 'neutral', dot = false, className = '', children, ...props }) {
  const tone = SEMANTIC[variant] || SEMANTIC.neutral;
  return (
    <span className={`${BASE} ${tone} ${className}`} {...props}>
      {dot && <span className="w-1.5 h-1.5 rounded-full bg-current opacity-70" aria-hidden="true" />}
      {children}
    </span>
  );
}
