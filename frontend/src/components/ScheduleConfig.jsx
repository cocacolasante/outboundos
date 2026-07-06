const DAYS = [
  { value: 0, label: 'Mon' },
  { value: 1, label: 'Tue' },
  { value: 2, label: 'Wed' },
  { value: 3, label: 'Thu' },
  { value: 4, label: 'Fri' },
  { value: 5, label: 'Sat' },
  { value: 6, label: 'Sun' },
];

const TIMEZONES = [
  'UTC',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Asia/Kolkata',
  'Australia/Sydney',
];

export default function ScheduleConfig({ value, onChange }) {
  function toggleDay(day) {
    const set = new Set(value.schedule_days);
    if (set.has(day)) set.delete(day);
    else set.add(day);
    onChange({ ...value, schedule_days: Array.from(set).sort((a, b) => a - b) });
  }

  // min_delay UI unit: derived. If a multiple of 60 and value >= 60, show minutes.
  const delayInMinutes = value.min_delay_unit === 'minutes';
  const displayDelay = delayInMinutes
    ? Math.round(value.min_delay_seconds / 60)
    : value.min_delay_seconds;

  function setDelayValue(displayValue) {
    const display = Number(displayValue) || 0;
    const seconds = delayInMinutes ? display * 60 : display;
    onChange({ ...value, min_delay_seconds: seconds });
  }

  function setDelayUnit(unit) {
    // Switching unit preserves min_delay_seconds — only the display changes.
    onChange({ ...value, min_delay_unit: unit });
  }

  return (
    <div data-testid="schedule-config" className="grid gap-4">
      <div>
        <label className="block text-sm font-medium text-slate-700 mb-2">Days</label>
        <div role="group" aria-label="Days of week" className="flex gap-2 flex-wrap">
          {DAYS.map((d) => {
            const active = value.schedule_days.includes(d.value);
            return (
              <button
                key={d.value}
                type="button"
                aria-pressed={active}
                aria-label={d.label}
                onClick={() => toggleDay(d.value)}
                className={`px-3 py-1.5 text-xs rounded-full border font-medium transition-colors ${
                  active
                    ? 'bg-brand-600 text-white border-brand-600'
                    : 'border-slate-300 text-slate-600 hover:border-brand-400 bg-white'
                }`}
              >
                {d.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-slate-700 mb-1">Start time</label>
          <input
            type="time"
            aria-label="Start time"
            value={value.schedule_time_start.slice(0, 5)}
            onChange={(e) => onChange({ ...value, schedule_time_start: e.target.value })}
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
          />
        </div>
        <div className="flex-1">
          <label className="block text-sm font-medium text-slate-700 mb-1">End time</label>
          <input
            type="time"
            aria-label="End time"
            value={value.schedule_time_end.slice(0, 5)}
            onChange={(e) => onChange({ ...value, schedule_time_end: e.target.value })}
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
          />
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-700 mb-1">Timezone</label>
        <select
          aria-label="Timezone"
          value={value.schedule_timezone}
          onChange={(e) => onChange({ ...value, schedule_timezone: e.target.value })}
          className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
        >
          {TIMEZONES.map((tz) => (
            <option key={tz} value={tz}>{tz}</option>
          ))}
        </select>
      </div>

      <div className="flex gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-slate-700 mb-1">Max per hour</label>
          <input
            type="number"
            min="1"
            aria-label="Max per hour"
            placeholder="No limit"
            value={value.max_per_hour ?? ''}
            onChange={(e) =>
              onChange({
                ...value,
                max_per_hour: e.target.value === '' ? null : Number(e.target.value),
              })
            }
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
          />
        </div>
        <div className="flex-1">
          <label className="block text-sm font-medium text-slate-700 mb-1">Max per day</label>
          <input
            type="number"
            min="1"
            aria-label="Max per day"
            placeholder="No limit"
            value={value.max_per_day ?? ''}
            onChange={(e) =>
              onChange({
                ...value,
                max_per_day: e.target.value === '' ? null : Number(e.target.value),
              })
            }
            className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
          />
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-700 mb-1">Minimum delay between sends</label>
        <div className="flex gap-2">
          <input
            type="number"
            min="0"
            aria-label="Min delay"
            value={displayDelay}
            onChange={(e) => setDelayValue(e.target.value)}
            className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
          />
          <select
            aria-label="Min delay unit"
            value={delayInMinutes ? 'minutes' : 'seconds'}
            onChange={(e) => setDelayUnit(e.target.value)}
            className="w-28 px-3 py-2 border border-slate-300 rounded-lg text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white"
          >
            <option value="seconds">seconds</option>
            <option value="minutes">minutes</option>
          </select>
        </div>
      </div>
    </div>
  );
}
