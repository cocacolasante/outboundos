import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import UiKit from './UiKit.jsx';

describe('UiKit gallery', () => {
  it('renders the primitive gallery without crashing', () => {
    render(<UiKit />);
    expect(screen.getByTestId('ui-kit')).toBeInTheDocument();
    // a sampling of primitives present
    expect(screen.getByText('Primary')).toBeInTheDocument();
    expect(screen.getByTestId('kit-table')).toBeInTheDocument();
  });

  it('opens the example modal', () => {
    render(<UiKit />);
    fireEvent.click(screen.getByTestId('open-modal'));
    expect(screen.getByTestId('modal')).toBeInTheDocument();
  });
});
