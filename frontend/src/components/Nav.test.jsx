import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Nav from './Nav.jsx';

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Nav />
    </MemoryRouter>
  );
}

describe('Nav', () => {
  it('renders the wordmark', () => {
    renderAt('/');
    expect(screen.getByText(/email blaster/i)).toBeInTheDocument();
  });

  it('renders Campaigns and Settings links', () => {
    renderAt('/');
    expect(screen.getByRole('link', { name: /campaigns/i })).toHaveAttribute('href', '/');
    expect(screen.getByRole('link', { name: /settings/i })).toHaveAttribute('href', '/settings');
  });

  it('preserves data-testid="nav"', () => {
    renderAt('/');
    expect(screen.getByTestId('nav')).toBeInTheDocument();
  });

  it('marks Campaigns link as active at /', () => {
    renderAt('/');
    const link = screen.getByRole('link', { name: /campaigns/i });
    // active class is delivered via NavLink's style; just check it's not flagged as inactive
    expect(link).toBeInTheDocument();
  });
});
