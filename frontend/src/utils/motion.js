/**
 * Shared framer-motion presets (Phase 1, UI refinement).  One place for the
 * spring transitions + enter/exit variants so every animated surface (modals,
 * sheets, dropdowns, list items, mounts) feels the same — fast, springy,
 * purposeful.
 *
 * Reduced motion: wrap the app in <MotionConfig reducedMotion="user"> (done in
 * App.jsx); framer-motion then auto-snaps transform/opacity to their target
 * for users with `prefers-reduced-motion: reduce`.  Components that animate
 * outside framer-motion gate with `useReducedMotion()` / `motion-safe:`.
 */

// Transitions ---------------------------------------------------------------

/** Default UI spring — snappy but soft. */
export const spring = { type: 'spring', stiffness: 420, damping: 32, mass: 0.9 };

/** Quicker spring for small controls (menus, toggles). */
export const springFast = { type: 'spring', stiffness: 550, damping: 34 };

/** Gentle spring for larger surfaces (sheets, page sections). */
export const springGentle = { type: 'spring', stiffness: 280, damping: 30 };

/** Simple tween for opacity-only fades (no overshoot). */
export const fade = { duration: 0.16, ease: [0.4, 0, 0.2, 1] };

// Variants ------------------------------------------------------------------

/** Backdrop / overlay fade. */
export const overlayVariants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: fade },
  exit: { opacity: 0, transition: fade },
};

/** Modal/dialog: scale + fade up into place. */
export const modalVariants = {
  hidden: { opacity: 0, scale: 0.96, y: 8 },
  visible: { opacity: 1, scale: 1, y: 0, transition: spring },
  exit: { opacity: 0, scale: 0.97, y: 6, transition: fade },
};

/** Dropdown / menu / popover: small scale + fade from the top edge. */
export const popVariants = {
  hidden: { opacity: 0, scale: 0.97, y: -4 },
  visible: { opacity: 1, scale: 1, y: 0, transition: springFast },
  exit: { opacity: 0, scale: 0.98, y: -2, transition: fade },
};

/** Mount / list-item entrance (use with a small stagger). */
export const riseVariants = {
  hidden: { opacity: 0, y: 8 },
  visible: { opacity: 1, y: 0, transition: spring },
  exit: { opacity: 0, y: 4, transition: fade },
};

/** Press feedback for tactile buttons/cards (whileTap). */
export const tap = { scale: 0.97 };
