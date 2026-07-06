import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  Input, Field, Toggle, Checkbox, Badge, Tabs, Tooltip, Menu, Modal, Table,
} from './ui.jsx';

describe('Field + Input', () => {
  it('wires label, required mark, and inline error (role=alert) with aria', () => {
    render(
      <Field label="Amount" required error="Required">
        {(p) => <Input data-testid="inp" {...p} />}
      </Field>,
    );
    const input = screen.getByTestId('inp');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    const err = screen.getByRole('alert');
    expect(err).toHaveTextContent('Required');
    // control is described by the error message
    expect(input.getAttribute('aria-describedby')).toContain(err.id);
    // label is associated
    expect(screen.getByText('Amount').closest('label')).toHaveAttribute('for', input.id);
  });
});

describe('Toggle', () => {
  it('is a switch and toggles aria-checked on click', () => {
    const onChange = vi.fn();
    const { rerender } = render(<Toggle checked={false} onChange={onChange} label="N" />);
    const sw = screen.getByRole('switch');
    expect(sw).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
    rerender(<Toggle checked onChange={onChange} label="N" />);
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true');
  });
});

describe('Checkbox + Badge', () => {
  it('checkbox renders with a label', () => {
    render(<Checkbox label="Subscribe" />);
    expect(screen.getByLabelText('Subscribe')).toBeInTheDocument();
  });
  it('badge applies the semantic variant tone', () => {
    render(<Badge variant="success">Won</Badge>);
    expect(screen.getByText('Won').className).toMatch(/success/);
  });
});

describe('Tabs', () => {
  it('renders, click changes active, ArrowRight moves selection', async () => {
    const onChange = vi.fn();
    const tabs = [{ key: 'a', label: 'A' }, { key: 'b', label: 'B' }, { key: 'c', label: 'C' }];
    const { rerender } = render(<Tabs tabs={tabs} active="a" onChange={onChange} />);
    expect(screen.getByTestId('tabs-a')).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByTestId('tabs-b'));
    expect(onChange).toHaveBeenCalledWith('b');
    // keyboard: ArrowRight from active 'a' -> 'b'
    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole('tablist'), { key: 'ArrowRight' });
    expect(onChange).toHaveBeenCalledWith('b');
    // roving tabindex
    rerender(<Tabs tabs={tabs} active="b" onChange={onChange} />);
    expect(screen.getByTestId('tabs-b')).toHaveAttribute('tabindex', '0');
    expect(screen.getByTestId('tabs-a')).toHaveAttribute('tabindex', '-1');
  });
});

describe('Tooltip', () => {
  it('shows on focus and hides on blur', () => {
    render(<Tooltip label="hint"><button type="button">x</button></Tooltip>);
    expect(screen.queryByRole('tooltip')).toBeNull();
    fireEvent.focus(screen.getByRole('button'));
    expect(screen.getByRole('tooltip')).toHaveTextContent('hint');
    fireEvent.blur(screen.getByRole('button'));
    expect(screen.queryByRole('tooltip')).toBeNull();
  });
});

describe('Menu', () => {
  it('opens on trigger, fires onSelect, closes', async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<Menu label="Actions" items={[{ label: 'Edit', onSelect }, { label: 'Delete', danger: true, onSelect: () => {} }]} />);
    const trigger = screen.getByTestId('menu-trigger');
    await user.click(trigger);
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await user.click(screen.getByTestId('menu-item-0'));
    expect(onSelect).toHaveBeenCalled();
    // closes (state flips synchronously; the node animates out via AnimatePresence)
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });
});

describe('Modal', () => {
  it('renders nothing when closed', () => {
    render(<Modal open={false} onClose={() => {}} title="T">body</Modal>);
    expect(screen.queryByTestId('modal')).toBeNull();
  });
  it('renders a dialog, closes on close-button and Escape', () => {
    const onClose = vi.fn();
    render(<Modal open onClose={onClose} title="My dialog">body</Modal>);
    const dialog = screen.getByTestId('modal');
    expect(dialog).toHaveAttribute('role', 'dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    fireEvent.click(screen.getByTestId('modal-close'));
    expect(onClose).toHaveBeenCalled();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe('Table', () => {
  const cols = [
    { key: 'name', label: 'Name', sortable: true },
    { key: 'amt', label: 'Amount', numeric: true, sortable: true },
  ];
  it('renders rows; sortable header fires onSort; numeric cells are tabular', () => {
    const onSort = vi.fn();
    render(<Table testId="t" columns={cols} rows={[{ id: 1, name: 'Acme', amt: 100 }]} sort={{ key: 'amt', dir: 'desc' }} onSort={onSort} />);
    expect(screen.getAllByTestId('t-row')).toHaveLength(1);
    fireEvent.click(screen.getByTestId('t-sort-name'));
    expect(onSort).toHaveBeenCalledWith('name');
    // numeric column header carries aria-sort when active
    const amtHeader = screen.getByTestId('t-sort-amt').closest('th');
    expect(amtHeader).toHaveAttribute('aria-sort', 'descending');
  });
  it('shows the empty state', () => {
    render(<Table testId="t" columns={cols} rows={[]} empty="Nothing." />);
    expect(screen.getByTestId('t-empty')).toHaveTextContent('Nothing.');
  });
});
