import { createContext, useCallback, useContext, useState } from 'react';

const ToastContext = createContext(null);

const TYPE_CLASSES = {
  success: 'bg-emerald-600 text-white',
  error:   'bg-red-600 text-white',
  info:    'bg-slate-800 text-white',
};

const TYPE_ICONS = {
  success: '✓',
  error:   '✕',
  info:    'ℹ',
};

export function ToastProvider({ children, defaultDuration = 4000 }) {
  const [toasts, setToasts] = useState([]);

  const remove = useCallback((id) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const push = useCallback((message, type = 'info', duration = defaultDuration) => {
    const id =
      typeof crypto !== 'undefined' && crypto.randomUUID
        ? crypto.randomUUID()
        : `t-${Math.random().toString(36).slice(2)}-${Date.now()}`;
    setToasts((current) => [...current, { id, message, type }]);
    if (duration > 0) {
      setTimeout(() => remove(id), duration);
    }
    return id;
  }, [defaultDuration, remove]);

  const api = {
    success: (m, d) => push(m, 'success', d),
    error: (m, d) => push(m, 'error', d),
    info: (m, d) => push(m, 'info', d),
    dismiss: remove,
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div data-testid="toast-container" className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
        {toasts.map((t) => {
          const colorClass = TYPE_CLASSES[t.type] || TYPE_CLASSES.info;
          const icon = TYPE_ICONS[t.type] || TYPE_ICONS.info;
          return (
            <div
              key={t.id}
              data-testid="toast"
              data-toast-type={t.type}
              className={`flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg text-sm font-medium min-w-64 max-w-sm cursor-pointer ${colorClass}`}
              onClick={() => remove(t.id)}
            >
              <span>{icon}</span>
              <span className="flex-1">{t.message}</span>
              <button
                type="button"
                className="ml-auto text-white/70 hover:text-white"
                onClick={() => remove(t.id)}
                aria-label="Dismiss"
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  // Tolerant: missing provider returns no-op API so components don't crash
  // in isolated test renders.
  return (
    useContext(ToastContext) || {
      success: () => {},
      error: () => {},
      info: () => {},
      dismiss: () => {},
    }
  );
}
