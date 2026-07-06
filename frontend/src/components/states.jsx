/**
 * Shared loading / empty / error states so every analytics + CRM surface
 * presents the same skeletons and messaging (Phase 4 consolidation).
 */

/** A single shimmer bar. Honors prefers-reduced-motion via Tailwind's
 *  motion-safe variant (animate-pulse only when motion is allowed). */
export function Skeleton({ className = '' }) {
  return (
    <div
      data-testid="skeleton"
      className={`bg-slate-200/70 rounded motion-safe:animate-pulse ${className}`}
    />
  );
}

/** Card-shaped loading placeholder grid. */
export function LoadingCards({ count = 4, testId = 'loading-cards' }) {
  return (
    <div data-testid={testId} className="grid grid-cols-2 md:grid-cols-4 gap-4">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="bg-white rounded-xl border border-slate-200 p-4">
          <Skeleton className="h-3 w-1/2 mb-3" />
          <Skeleton className="h-6 w-3/4" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ title = 'Nothing here yet', hint, icon = '📭', testId = 'empty-state' }) {
  return (
    <div data-testid={testId} className="text-center py-12 px-4">
      <div className="text-3xl mb-2" aria-hidden="true">{icon}</div>
      <div className="text-sm font-medium text-slate-600">{title}</div>
      {hint && <div className="text-xs text-slate-400 mt-1">{hint}</div>}
    </div>
  );
}

export function ErrorState({ message = 'Something went wrong', onRetry, testId = 'error-state' }) {
  return (
    <div data-testid={testId} className="text-center py-10 px-4">
      <div className="text-sm font-medium text-red-600">{message}</div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 px-3 py-1.5 text-xs font-medium border border-slate-300 rounded-md text-slate-700 hover:bg-slate-50"
        >
          Retry
        </button>
      )}
    </div>
  );
}
