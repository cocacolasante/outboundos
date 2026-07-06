/**
 * Modal / Sheet primitive (Phase 2, UI refinement).  Consolidates the 8
 * hand-rolled `fixed inset-0 bg-black/50` dialogs into one accessible,
 * animated component: portal + backdrop, spring enter/exit, Esc + backdrop
 * close, focus moved in on open and restored on close, focus trapped while
 * open, `role="dialog"` + `aria-modal`.  Reduced motion handled by
 * framer-motion's MotionConfig (App-level).
 */
import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'framer-motion';

import { modalVariants, overlayVariants } from '../../utils/motion.js';

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])';

const SIZES = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-2xl', xl: 'max-w-4xl' };

export function Modal({
  open, onClose, title, children, footer,
  size = 'md', testId = 'modal', closeOnBackdrop = true,
}) {
  const panelRef = useRef(null);
  const restoreRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    restoreRef.current = document.activeElement;
    // Move focus into the dialog (first focusable, else the panel).
    const t = setTimeout(() => {
      const panel = panelRef.current;
      if (!panel) return;
      const first = panel.querySelector(FOCUSABLE);
      (first || panel).focus();
    }, 0);

    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); onClose?.(); return; }
      if (e.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const items = panel.querySelectorAll(FOCUSABLE);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', onKey, true);
    return () => {
      clearTimeout(t);
      document.removeEventListener('keydown', onKey, true);
      // Restore focus to the trigger.
      if (restoreRef.current?.focus) restoreRef.current.focus();
    };
  }, [open, onClose]);

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/40 backdrop-blur-[2px]"
          variants={overlayVariants}
          initial="hidden" animate="visible" exit="exit"
          onMouseDown={(e) => { if (closeOnBackdrop && e.target === e.currentTarget) onClose?.(); }}
        >
          <motion.div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-label={typeof title === 'string' ? title : undefined}
            tabIndex={-1}
            data-testid={testId}
            variants={modalVariants}
            initial="hidden" animate="visible" exit="exit"
            onMouseDown={(e) => e.stopPropagation()}
            className={`w-full ${SIZES[size] || SIZES.md} bg-white rounded-card shadow-overlay
                        max-h-[90vh] flex flex-col focus:outline-none`}
          >
            {title && (
              <div className="flex items-start justify-between gap-4 px-6 pt-5 pb-3 border-b border-slate-100">
                <h2 className="m-0 text-base font-semibold text-slate-900">{title}</h2>
                <button
                  type="button" onClick={onClose} aria-label="Close"
                  data-testid={`${testId}-close`}
                  className="text-slate-400 hover:text-slate-600 text-xl leading-none -mt-0.5 p-1
                             rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
                >
                  ×
                </button>
              </div>
            )}
            <div className="px-6 py-4 overflow-y-auto">{children}</div>
            {footer && (
              <div className="px-6 py-3 border-t border-slate-100 flex justify-end gap-2">{footer}</div>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
