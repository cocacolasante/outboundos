import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('recharts', () => {
  const noop = ({ children }) => <div>{children}</div>;
  return {
    BarChart: ({ children }) => <div data-testid="bar-chart">{children}</div>,
    Bar: () => <div />, LineChart: ({ children }) => <div>{children}</div>, Line: () => <div />,
    PieChart: ({ children }) => <div>{children}</div>, Pie: () => <div />, Cell: () => <div />,
    XAxis: noop, YAxis: noop, CartesianGrid: noop, Tooltip: noop, Legend: noop,
    ResponsiveContainer: noop,
  };
});

vi.mock('../api/reportBuilder.js', () => ({
  getReportMetadata: vi.fn(),
  listReports: vi.fn(),
  getReport: vi.fn(),
  createReport: vi.fn(),
  updateReport: vi.fn(),
  duplicateReport: vi.fn(),
  deleteReport: vi.fn(),
  runAdhocReport: vi.fn(),
  runSavedReport: vi.fn(),
}));

import * as api from '../api/reportBuilder.js';
import ReportBuilder from './ReportBuilder.jsx';
import { ToastProvider } from '../components/Toast.jsx';

const METADATA = {
  relative_ranges: ['last_30_days', 'this_year'],
  objects: [
    {
      key: 'opportunities', label: 'Opportunities', default_columns: ['name', 'stage', 'amount'],
      fields: [
        { key: 'name', label: 'Name', type: 'string', operators: ['equals', 'contains'], aggregates: ['count'], groupable: true, enum_values: null },
        { key: 'stage', label: 'Stage', type: 'enum', operators: ['equals', 'in'], aggregates: ['count'], groupable: true, enum_values: ['proposal', 'negotiation'] },
        { key: 'amount', label: 'Amount', type: 'number', operators: ['gte', 'between'], aggregates: ['sum', 'avg', 'count'], groupable: true, enum_values: null },
      ],
    },
    {
      key: 'leads', label: 'Leads', default_columns: ['email'],
      fields: [{ key: 'email', label: 'Email', type: 'string', operators: ['contains'], aggregates: ['count'], groupable: true, enum_values: null }],
    },
  ],
};

const TABULAR_RESULT = {
  data_source: 'opportunities',
  columns: [
    { key: 'name', label: 'Name', type: 'string' },
    { key: 'stage', label: 'Stage', type: 'enum' },
    { key: 'amount', label: 'Amount', type: 'number' },
  ],
  rows: [{ name: 'Acme big', stage: 'proposal', amount: 10000 }],
  row_count: 1, grouped: false, truncated: false, limit: 1000,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider defaultDuration={0}>
        <ReportBuilder />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getReportMetadata.mockResolvedValue(METADATA);
  api.listReports.mockResolvedValue([]);
  api.runAdhocReport.mockResolvedValue(TABULAR_RESULT);
});

async function selectOpportunities(user) {
  await screen.findByTestId('report-builder');
  await waitFor(() => expect(screen.getByTestId('data-source-select')).not.toBeDisabled?.());
  await user.selectOptions(screen.getByTestId('data-source-select'), 'opportunities');
}

describe('ReportBuilder', () => {
  it('lists data sources from metadata', async () => {
    renderPage();
    const sel = await screen.findByTestId('data-source-select');
    await waitFor(() => expect(within(sel).getByText('Opportunities')).toBeInTheDocument());
    expect(within(sel).getByText('Leads')).toBeInTheDocument();
  });

  it('picking a source selects default columns and runs a tabular report', async () => {
    const user = userEvent.setup();
    renderPage();
    await selectOpportunities(user);
    // default columns toggled on
    await waitFor(() => expect(screen.getByTestId('column-toggle-name')).toHaveAttribute('aria-pressed', 'true'));

    await user.click(screen.getByTestId('run-btn'));
    await waitFor(() => expect(api.runAdhocReport).toHaveBeenCalled());
    const payload = api.runAdhocReport.mock.calls[0][0];
    expect(payload.data_source).toBe('opportunities');
    expect(payload.definition.columns).toEqual(['name', 'stage', 'amount']);

    // results render
    const table = await screen.findByTestId('results-table');
    expect(within(table).getByText('Acme big')).toBeInTheDocument();
    expect(screen.getByTestId('result-col-amount')).toBeInTheDocument();
  });

  it('exposes a11y attributes: aria-pressed chips + labelled remove buttons', async () => {
    const user = userEvent.setup();
    renderPage();
    await selectOpportunities(user);
    // default columns are toggled "on" -> aria-pressed reflects state
    expect(screen.getByTestId('column-toggle-name')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('column-toggle-amount')).toHaveAttribute('aria-pressed', 'true');
    // icon-only remove button has an accessible name
    await user.click(screen.getByTestId('add-filter-btn'));
    expect(screen.getByTestId('remove-filter-0')).toHaveAttribute('aria-label', 'Remove filter');
  });

  it('adds a filter and includes it in the run definition', async () => {
    const user = userEvent.setup();
    renderPage();
    await selectOpportunities(user);
    await user.click(screen.getByTestId('add-filter-btn'));
    await user.selectOptions(screen.getByTestId('filter-field-0'), 'amount');
    await user.selectOptions(screen.getByTestId('filter-op-0'), 'gte');
    await user.type(screen.getByTestId('filter-value'), '5000');

    await user.click(screen.getByTestId('run-btn'));
    await waitFor(() => expect(api.runAdhocReport).toHaveBeenCalled());
    const { definition } = api.runAdhocReport.mock.calls.at(-1)[0];
    expect(definition.filters).toEqual([{ field: 'amount', op: 'gte', value: '5000' }]);
  });

  it('summary mode sends group_by + aggregates', async () => {
    const user = userEvent.setup();
    renderPage();
    await selectOpportunities(user);
    await user.click(screen.getByTestId('mode-summary'));
    await user.click(screen.getByTestId('groupby-toggle-stage'));
    await user.click(screen.getByTestId('add-aggregate-btn'));
    await user.selectOptions(screen.getByTestId('agg-fn-0'), 'sum');
    await user.selectOptions(screen.getByTestId('agg-field-0'), 'amount');

    await user.click(screen.getByTestId('run-btn'));
    await waitFor(() => expect(api.runAdhocReport).toHaveBeenCalled());
    const { definition } = api.runAdhocReport.mock.calls.at(-1)[0];
    expect(definition.group_by).toEqual(['stage']);
    expect(definition.aggregates).toEqual([{ fn: 'sum', field: 'amount' }]);
  });

  it('saves the report via createReport', async () => {
    api.createReport.mockResolvedValue({ id: 'r1', name: 'My report', data_source: 'opportunities', definition: {} });
    const user = userEvent.setup();
    renderPage();
    await selectOpportunities(user);
    await user.type(screen.getByTestId('report-name'), 'My report');
    await user.click(screen.getByTestId('save-report-btn'));
    await waitFor(() => expect(api.createReport).toHaveBeenCalled());
    expect(api.createReport.mock.calls[0][0].name).toBe('My report');
  });

  it('exports results to CSV', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:x');
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const user = userEvent.setup();
    renderPage();
    await selectOpportunities(user);
    await user.click(screen.getByTestId('run-btn'));
    await screen.findByTestId('results-table');
    await user.click(screen.getByTestId('export-csv-btn'));
    expect(createObjectURL).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('loads a saved report from the rail', async () => {
    api.listReports.mockResolvedValue([
      { id: 'r9', name: 'Saved one', data_source: 'leads', definition: { columns: ['email'] } },
    ]);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('saved-report-r9'));
    // data source switches to leads
    await waitFor(() => expect(screen.getByTestId('data-source-select')).toHaveValue('leads'));
  });
});
