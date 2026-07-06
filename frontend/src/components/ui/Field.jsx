/**
 * Form-control primitives (Phase 2, UI refinement).  Tokens only; consistent
 * focus-visible ring, invalid + disabled states, keyboard + ARIA.  Replaces
 * the ~50/49/20 hand-rolled input/select/textarea variants.
 */
import { forwardRef, useId } from 'react';

const CONTROL_BASE =
  'w-full px-3 py-2 rounded-lg border text-sm text-slate-900 bg-white ' +
  'transition-colors duration-fast placeholder:text-slate-400 ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:border-brand-500 ' +
  'disabled:opacity-60 disabled:bg-slate-50 disabled:cursor-not-allowed';

const borderFor = (invalid) =>
  invalid ? 'border-danger-400 focus-visible:ring-danger-500' : 'border-slate-300';

export const Input = forwardRef(function Input(
  { invalid = false, className = '', ...props }, ref,
) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={`${CONTROL_BASE} ${borderFor(invalid)} ${className}`}
      {...props}
    />
  );
});

export const Textarea = forwardRef(function Textarea(
  { invalid = false, className = '', rows = 4, ...props }, ref,
) {
  return (
    <textarea
      ref={ref}
      rows={rows}
      aria-invalid={invalid || undefined}
      className={`${CONTROL_BASE} font-[inherit] ${borderFor(invalid)} ${className}`}
      {...props}
    />
  );
});

export const Select = forwardRef(function Select(
  { invalid = false, className = '', children, ...props }, ref,
) {
  return (
    <select
      ref={ref}
      aria-invalid={invalid || undefined}
      className={`${CONTROL_BASE} pr-8 appearance-none bg-no-repeat bg-[length:1rem] bg-[right:0.5rem_center] ${borderFor(invalid)} ${className}`}
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20' fill='%2364748b'%3E%3Cpath fill-rule='evenodd' d='M5.23 7.21a.75.75 0 011.06.02L10 11.17l3.71-3.94a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z' clip-rule='evenodd'/%3E%3C/svg%3E\")",
      }}
      {...props}
    >
      {children}
    </select>
  );
});

/**
 * Field wrapper: label (+ required/optional), control, hint, and inline error.
 * Wires htmlFor + aria-describedby so the control is properly labelled.
 * Pass a render-prop child `(props) => <Input {...props} />` so the id +
 * aria wiring lands on the control; or a plain element (best-effort).
 */
export function Field({
  label, hint, error, required = false, optional = false, htmlFor, children,
  className = '',
}) {
  const autoId = useId();
  const id = htmlFor || autoId;
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-err` : undefined;
  const describedBy = [hintId, errId].filter(Boolean).join(' ') || undefined;

  const control = typeof children === 'function'
    ? children({ id, invalid: !!error, 'aria-describedby': describedBy })
    : children;

  return (
    <div className={className}>
      {label && (
        <label htmlFor={id} className="block text-xs font-medium text-slate-700 mb-1">
          {label}
          {required && <span className="text-danger-600 ml-0.5" aria-hidden="true">*</span>}
          {optional && <span className="text-slate-400 font-normal ml-1">(optional)</span>}
        </label>
      )}
      {control}
      {hint && !error && (
        <p id={hintId} className="text-xs text-slate-400 mt-1 mb-0">{hint}</p>
      )}
      {error && (
        <p id={errId} role="alert" className="text-xs text-danger-600 mt-1 mb-0">{error}</p>
      )}
    </div>
  );
}

export function Checkbox({ label, className = '', id, ...props }) {
  const autoId = useId();
  const cid = id || autoId;
  return (
    <label htmlFor={cid} className={`inline-flex items-center gap-2 text-sm text-slate-700 cursor-pointer ${className}`}>
      <input
        id={cid}
        type="checkbox"
        className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
        {...props}
      />
      {label && <span>{label}</span>}
    </label>
  );
}

export function Radio({ label, className = '', id, ...props }) {
  const autoId = useId();
  const rid = id || autoId;
  return (
    <label htmlFor={rid} className={`inline-flex items-center gap-2 text-sm text-slate-700 cursor-pointer ${className}`}>
      <input
        id={rid}
        type="radio"
        className="h-4 w-4 border-slate-300 text-brand-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
        {...props}
      />
      {label && <span>{label}</span>}
    </label>
  );
}

/** Accessible switch (role=switch). Spring knob honored via duration-fast. */
export function Toggle({ checked = false, onChange, label, disabled = false, className = '', ...props }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={typeof label === 'string' ? label : undefined}
      disabled={disabled}
      onClick={() => onChange?.(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-pill transition-colors duration-fast
        focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-1
        disabled:opacity-50 disabled:cursor-not-allowed
        ${checked ? 'bg-brand-600' : 'bg-slate-300'} ${className}`}
      {...props}
    >
      <span
        className={`inline-block h-4 w-4 rounded-full bg-white shadow transition-transform duration-fast ease-spring
          ${checked ? 'translate-x-4' : 'translate-x-0.5'}`}
      />
    </button>
  );
}
