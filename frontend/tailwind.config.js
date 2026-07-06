/** @type {import('tailwindcss').Config} */
// Design tokens (Phase 5).  ADDITIVE — formalizes the de-facto conventions
// (slate/blue/emerald palette, rounded-xl cards, soft shadows) into named
// tokens without changing any existing utility class.  New CRM/analytics UI
// uses these tokens (e.g. `shadow-card`, `rounded-card`, `bg-brand-600`,
// `duration-fast`, `ease-spring`) instead of one-off values.
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Brand scale aliased to the blue the app already uses, so existing
        // `blue-*` classes and new `brand-*` tokens render identically.
        brand: {
          50: '#eff6ff', 100: '#dbeafe', 200: '#bfdbfe', 300: '#93c5fd',
          400: '#60a5fa', 500: '#3b82f6', 600: '#2563eb', 700: '#1d4ed8',
          800: '#1e40af', 900: '#1e3a8a',
        },
        // Semantic scales (Phase 1, UI refinement) — aliased to the hues the
        // app already uses for status, so adoption is drop-in.  One source of
        // truth lives in src/utils/statusColors.js.
        success: {
          50: '#ecfdf5', 100: '#d1fae5', 200: '#a7f3d0', 500: '#10b981',
          600: '#059669', 700: '#047857',
        },
        warning: {
          50: '#fffbeb', 100: '#fef3c7', 200: '#fde68a', 500: '#f59e0b',
          600: '#d97706', 700: '#b45309',
        },
        danger: {
          50: '#fef2f2', 100: '#fee2e2', 200: '#fecaca', 500: '#ef4444',
          600: '#dc2626', 700: '#b91c1c',
        },
        info: {
          50: '#f0f9ff', 100: '#e0f2fe', 200: '#bae6fd', 500: '#0ea5e9',
          600: '#0284c7', 700: '#0369a1',
        },
      },
      borderRadius: {
        card: '0.75rem',   // = rounded-xl (the standard card radius)
        pill: '9999px',
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
        'card-hover': '0 4px 12px -2px rgb(0 0 0 / 0.10)',
        // Elevated overlays (modals, sheets, dropdowns) — retires the
        // ad-hoc shadow-2xl/shadow-xl used per-modal today.
        overlay: '0 10px 38px -10px rgb(0 0 0 / 0.35), 0 10px 20px -15px rgb(0 0 0 / 0.20)',
      },
      transitionDuration: {
        fast: '120ms',
        base: '200ms',
      },
      transitionTimingFunction: {
        // Gentle spring for drag/drop + reflow (honored only when motion is
        // allowed — components gate with motion-safe: / useReducedMotion).
        spring: 'cubic-bezier(0.34, 1.56, 0.64, 1)',
      },
    },
  },
  plugins: [],
}
