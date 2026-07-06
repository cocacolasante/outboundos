import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ScheduleConfig from './ScheduleConfig.jsx';

const DEFAULT = {
  schedule_days: [0, 1, 2, 3, 4],
  schedule_time_start: '09:00',
  schedule_time_end: '17:00',
  schedule_timezone: 'UTC',
  max_per_hour: null,
  max_per_day: null,
  min_delay_seconds: 60,
  min_delay_unit: 'seconds',
};

describe('ScheduleConfig', () => {
  it('renders all 7 day toggles', () => {
    render(<ScheduleConfig value={DEFAULT} onChange={() => {}} />);
    ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach((d) => {
      expect(screen.getByRole('button', { name: d })).toBeInTheDocument();
    });
  });

  it('marks weekday buttons as pressed by default', () => {
    render(<ScheduleConfig value={DEFAULT} onChange={() => {}} />);
    expect(screen.getByRole('button', { name: 'Mon' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Sat' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('clicking a day toggles it on/off via onChange', () => {
    const onChange = vi.fn();
    render(<ScheduleConfig value={DEFAULT} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Sat' }));
    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[0][0];
    expect(next.schedule_days).toContain(5);
  });

  it('clicking an already-selected day removes it', () => {
    const onChange = vi.fn();
    render(<ScheduleConfig value={DEFAULT} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Mon' }));
    const next = onChange.mock.calls[0][0];
    expect(next.schedule_days).not.toContain(0);
  });

  it('changes start time', () => {
    const onChange = vi.fn();
    render(<ScheduleConfig value={DEFAULT} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/start time/i), { target: { value: '08:00' } });
    expect(onChange).toHaveBeenCalled();
    expect(onChange.mock.calls[0][0].schedule_time_start).toBe('08:00');
  });

  it('changes timezone', () => {
    const onChange = vi.fn();
    render(<ScheduleConfig value={DEFAULT} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/timezone/i), {
      target: { value: 'America/New_York' },
    });
    expect(onChange).toHaveBeenCalled();
    expect(onChange.mock.calls[0][0].schedule_timezone).toBe('America/New_York');
  });

  it('changes max per hour', () => {
    const onChange = vi.fn();
    render(<ScheduleConfig value={DEFAULT} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/max per hour/i), { target: { value: '50' } });
    expect(onChange).toHaveBeenCalled();
    expect(onChange.mock.calls[0][0].max_per_hour).toBe(50);
  });

  it('empty max-per-hour value becomes null', () => {
    const value = { ...DEFAULT, max_per_hour: 50 };
    const onChange = vi.fn();
    render(<ScheduleConfig value={value} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/max per hour/i), { target: { value: '' } });
    expect(onChange.mock.calls[0][0].max_per_hour).toBeNull();
  });

  it('switching unit to minutes converts displayed value but stores seconds', () => {
    const onChange = vi.fn();
    render(<ScheduleConfig value={DEFAULT} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/min delay unit/i), { target: { value: 'minutes' } });
    // 60 seconds displayed → still stores 60 seconds (1 minute)
    const next = onChange.mock.calls[0][0];
    expect(next.min_delay_seconds).toBe(60);
    expect(next.min_delay_unit).toBe('minutes');
  });

  it('changing minutes value scales to seconds', () => {
    const value = { ...DEFAULT, min_delay_unit: 'minutes' };
    const onChange = vi.fn();
    render(<ScheduleConfig value={value} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/min delay/i, { selector: 'input' }), {
      target: { value: '5' },
    });
    expect(onChange.mock.calls[0][0].min_delay_seconds).toBe(300);
  });
});
