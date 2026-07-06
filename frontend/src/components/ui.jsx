/**
 * Shared UI primitives — the public barrel for the design system.
 *
 * Button/Card/PageHeader are defined here (the original Phase-5 seed); the
 * Phase-2 primitives live in ./ui/* and are re-exported below, so every
 * consumer imports from a single module: `import { Button, Modal, Input,
 * Tabs, Badge, Table, … } from '../components/ui.jsx'`.
 */

// Phase-2 primitive library (form controls, overlays, navigation, data).
export {
  Input, Textarea, Select, Field, Checkbox, Radio, Toggle,
} from './ui/Field.jsx';
export { Modal } from './ui/Modal.jsx';
export { Badge } from './ui/Badge.jsx';
export { Tabs } from './ui/Tabs.jsx';
export { Tooltip } from './ui/Tooltip.jsx';
export { Menu } from './ui/Menu.jsx';
export { Table } from './ui/Table.jsx';

const BTN_BASE =
  'inline-flex items-center justify-center gap-1.5 font-medium border rounded-lg ' +
  'transition-colors duration-fast ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 ' +
  'disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none';

const BTN_VARIANTS = {
  primary: 'bg-brand-600 text-white border-transparent hover:bg-brand-700 active:bg-brand-800 focus-visible:ring-brand-500',
  secondary: 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50 active:bg-slate-100 focus-visible:ring-brand-500',
  danger: 'bg-red-600 text-white border-transparent hover:bg-red-700 active:bg-red-800 focus-visible:ring-red-500',
  ghost: 'bg-transparent text-slate-600 border-transparent hover:bg-slate-100 active:bg-slate-200 focus-visible:ring-brand-500',
};

const BTN_SIZES = {
  sm: 'px-3 py-1.5 text-xs',
  md: 'px-4 py-2 text-sm',
};

function Spinner() {
  return (
    <svg className="w-3.5 h-3.5 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  );
}

/** The single button primitive.  Forwards data-testid / onClick / type / etc.
 *  ``loading`` shows a spinner and disables the button. */
export function Button({
  variant = 'primary', size = 'md', loading = false, disabled = false,
  className = '', children, ...props
}) {
  return (
    <button
      {...props}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`${BTN_BASE} ${BTN_VARIANTS[variant] || BTN_VARIANTS.primary} ${BTN_SIZES[size] || BTN_SIZES.md} ${className}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

/** Standard surface card. */
export function Card({ className = '', children, ...props }) {
  return (
    <div {...props} className={`bg-white rounded-card border border-slate-200 shadow-card ${className}`}>
      {children}
    </div>
  );
}

/** Consistent page scaffolding: title + optional subtitle + right-aligned
 *  actions slot. */
export function PageHeader({ title, subtitle, actions, testId }) {
  return (
    <div className="mb-5" data-testid={testId}>
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-bold text-slate-900 m-0">{title}</h1>
        {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      </div>
      {subtitle && <p className="text-sm text-slate-500 mt-1 mb-0">{subtitle}</p>}
    </div>
  );
}
