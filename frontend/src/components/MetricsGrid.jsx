/**
 * Simple 4-column metric card grid (collapses on narrow screens).
 *
 * Each metric: { label, value, tooltip?, accent? }
 */
export default function MetricsGrid({ metrics }) {
  return (
    <div
      data-testid="metrics-grid"
      className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4"
    >
      {metrics.map((m) => (
        <div
          key={m.label}
          data-testid={`metric-${m.label.toLowerCase().replace(/\s+/g, '-')}`}
          title={m.tooltip || ''}
          className="bg-white rounded-xl border border-slate-200 shadow-sm p-4"
        >
          <div className="text-xs text-slate-500 uppercase tracking-wide">
            {m.label}
          </div>
          <div
            className="text-2xl font-bold text-slate-900 mt-1"
            style={m.accent ? { color: m.accent } : undefined}
          >
            {m.value}
          </div>
          {m.tooltip && (
            <div className="text-xs text-slate-400 mt-1">{m.tooltip}</div>
          )}
        </div>
      ))}
    </div>
  );
}
