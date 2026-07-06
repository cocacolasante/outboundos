import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Button, Card, PageHeader } from './ui.jsx';

describe('Button primitive', () => {
  it('renders a button, forwards testid/onClick/children', () => {
    const onClick = vi.fn();
    render(<Button data-testid="b" onClick={onClick}>Go</Button>);
    const btn = screen.getByTestId('b');
    expect(btn.tagName).toBe('BUTTON');
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalled();
  });

  it('has focus-visible ring + hover + active state classes', () => {
    render(<Button data-testid="b">Go</Button>);
    const cls = screen.getByTestId('b').className;
    expect(cls).toMatch(/focus-visible:ring/);
    expect(cls).toMatch(/hover:/);
    expect(cls).toMatch(/active:/);
    expect(cls).toMatch(/disabled:opacity-50/);
  });

  it('loading disables the button and shows a spinner', () => {
    const onClick = vi.fn();
    render(<Button data-testid="b" loading onClick={onClick}>Save</Button>);
    const btn = screen.getByTestId('b');
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('aria-busy', 'true');
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('disabled prevents clicks', () => {
    const onClick = vi.fn();
    render(<Button data-testid="b" disabled onClick={onClick}>X</Button>);
    fireEvent.click(screen.getByTestId('b'));
    expect(onClick).not.toHaveBeenCalled();
  });

  it('variants apply distinct styling', () => {
    const { rerender } = render(<Button data-testid="b" variant="danger">D</Button>);
    expect(screen.getByTestId('b').className).toMatch(/bg-red-600/);
    rerender(<Button data-testid="b" variant="secondary">S</Button>);
    expect(screen.getByTestId('b').className).toMatch(/bg-white/);
  });
});

describe('Card / PageHeader', () => {
  it('Card renders children with the token card classes', () => {
    render(<Card data-testid="c">inside</Card>);
    const c = screen.getByTestId('c');
    expect(c).toHaveTextContent('inside');
    expect(c.className).toMatch(/shadow-card/);
    expect(c.className).toMatch(/rounded-card/);
  });

  it('PageHeader shows title, subtitle, and actions', () => {
    render(<PageHeader title="Deals" subtitle="your pipeline" actions={<button type="button">New</button>} />);
    expect(screen.getByRole('heading', { name: 'Deals' })).toBeInTheDocument();
    expect(screen.getByText('your pipeline')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'New' })).toBeInTheDocument();
  });
});
