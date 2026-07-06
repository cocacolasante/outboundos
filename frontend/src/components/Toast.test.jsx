import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { ToastProvider, useToast } from './Toast.jsx';

function Probe({ onReady }) {
  const toast = useToast();
  onReady(toast);
  return null;
}

describe('Toast', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows a success toast', () => {
    let api;
    render(
      <ToastProvider defaultDuration={0}>
        <Probe onReady={(t) => { api = t; }} />
      </ToastProvider>
    );
    act(() => { api.success('Saved!'); });
    const t = screen.getByTestId('toast');
    expect(t).toHaveTextContent(/saved/i);
    expect(t).toHaveAttribute('data-toast-type', 'success');
  });

  it('shows an error toast', () => {
    let api;
    render(
      <ToastProvider defaultDuration={0}>
        <Probe onReady={(t) => { api = t; }} />
      </ToastProvider>
    );
    act(() => { api.error('Broken'); });
    expect(screen.getByTestId('toast')).toHaveAttribute('data-toast-type', 'error');
  });

  it('stacks multiple toasts', () => {
    let api;
    render(
      <ToastProvider defaultDuration={0}>
        <Probe onReady={(t) => { api = t; }} />
      </ToastProvider>
    );
    act(() => {
      api.info('one');
      api.info('two');
    });
    expect(screen.getAllByTestId('toast')).toHaveLength(2);
  });

  it('auto-dismisses after duration', () => {
    let api;
    render(
      <ToastProvider defaultDuration={1000}>
        <Probe onReady={(t) => { api = t; }} />
      </ToastProvider>
    );
    act(() => { api.success('Bye'); });
    expect(screen.getByTestId('toast')).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1500); });
    expect(screen.queryByTestId('toast')).not.toBeInTheDocument();
  });

  it('useToast outside provider returns no-op api', () => {
    function Inner() {
      const t = useToast();
      // Calls should not throw
      t.success('x');
      t.error('y');
      t.info('z');
      return <div>ok</div>;
    }
    render(<Inner />);
    expect(screen.getByText('ok')).toBeInTheDocument();
  });
});
