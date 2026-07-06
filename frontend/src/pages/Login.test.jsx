import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Login from './Login.jsx';

vi.mock('../api/auth.js', () => ({
  login: vi.fn(),
  register: vi.fn(),
  forgotPassword: vi.fn(),
  resetPassword: vi.fn(),
  acceptInvite: vi.fn(),
}));

import { login, register, forgotPassword, resetPassword, acceptInvite } from '../api/auth.js';

function renderLogin(initialEntry = '/login') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={<div data-testid="app-home" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Login', () => {
  it('signs in and navigates into the app', async () => {
    login.mockResolvedValue({ user_id: 'u-1' });
    renderLogin();

    fireEvent.change(screen.getByTestId('login-email'), {
      target: { value: 'owner@example.com' },
    });
    fireEvent.change(screen.getByTestId('login-password'), {
      target: { value: 'hunter2hunter2' },
    });
    fireEvent.click(screen.getByTestId('login-submit'));

    await waitFor(() => expect(login).toHaveBeenCalledWith({
      email: 'owner@example.com',
      password: 'hunter2hunter2',
    }));
    expect(await screen.findByTestId('app-home')).toBeInTheDocument();
  });

  it('shows the backend detail on a failed sign-in', async () => {
    login.mockRejectedValue({
      response: { status: 401, data: { detail: 'invalid email or password' } },
    });
    renderLogin();

    fireEvent.change(screen.getByTestId('login-email'), {
      target: { value: 'owner@example.com' },
    });
    fireEvent.change(screen.getByTestId('login-password'), {
      target: { value: 'wrong' },
    });
    fireEvent.click(screen.getByTestId('login-submit'));

    expect(await screen.findByTestId('login-error')).toHaveTextContent(
      /invalid email or password/i
    );
  });

  it('signup mode reveals workspace name and registers', async () => {
    register.mockResolvedValue({ user_id: 'u-1' });
    renderLogin();

    fireEvent.click(screen.getByTestId('login-mode-signup'));
    expect(screen.getByTestId('signup-tenant-name')).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('login-email'), {
      target: { value: 'new@example.com' },
    });
    fireEvent.change(screen.getByTestId('signup-tenant-name'), {
      target: { value: 'Acme' },
    });
    fireEvent.change(screen.getByTestId('login-password'), {
      target: { value: 'hunter2hunter2' },
    });
    fireEvent.click(screen.getByTestId('login-submit'));

    await waitFor(() => expect(register).toHaveBeenCalledWith({
      email: 'new@example.com',
      password: 'hunter2hunter2',
      tenantName: 'Acme',
    }));
  });

  it('forgot mode sends the reset email and confirms neutrally', async () => {
    forgotPassword.mockResolvedValue({ ok: true });
    renderLogin();

    fireEvent.click(screen.getByTestId('login-mode-forgot'));
    fireEvent.change(screen.getByTestId('login-email'), {
      target: { value: 'owner@example.com' },
    });
    fireEvent.click(screen.getByTestId('login-submit'));

    await waitFor(() => expect(forgotPassword).toHaveBeenCalledWith('owner@example.com'));
    expect(await screen.findByTestId('login-notice')).toHaveTextContent(/reset link/i);
  });

  it('reset mode (via ?reset_token=) sets the new password then returns to sign-in', async () => {
    resetPassword.mockResolvedValue({ ok: true });
    renderLogin('/login?reset_token=tok-123');

    // Reset mode: no email field, just the new password.
    expect(screen.queryByTestId('login-email')).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId('login-password'), {
      target: { value: 'a-new-password-9' },
    });
    fireEvent.click(screen.getByTestId('login-submit'));

    await waitFor(() => expect(resetPassword).toHaveBeenCalledWith({
      token: 'tok-123',
      password: 'a-new-password-9',
    }));
    expect(await screen.findByTestId('login-notice')).toHaveTextContent(/sign in/i);
    // Back in login mode — the email field is back.
    expect(screen.getByTestId('login-email')).toBeInTheDocument();
  });
});

describe('Invite acceptance', () => {
  it('invite mode (via ?invite_token=) sets the password then returns to sign-in', async () => {
    acceptInvite.mockResolvedValue({ ok: true });
    renderLogin('/login?invite_token=inv-123');

    expect(screen.getByRole('heading', { name: /join the workspace/i })).toBeInTheDocument();
    expect(screen.queryByTestId('login-email')).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId('login-password'), {
      target: { value: 'my-new-password-1' },
    });
    fireEvent.click(screen.getByTestId('login-submit'));

    await waitFor(() => expect(acceptInvite).toHaveBeenCalledWith({
      token: 'inv-123', password: 'my-new-password-1',
    }));
    expect(await screen.findByTestId('login-notice')).toHaveTextContent(/sign in/i);
  });
});
