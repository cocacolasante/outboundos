/**
 * Shared chart components — one consistent palette / axes / tooltip / empty
 * state across the report builder, dashboard, and analytics surfaces.
 * Thin wrappers over recharts so callers don't re-wire axes each time.
 */
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';

import { EmptyState } from './states.jsx';

export const CHART_PALETTE = [
  '#2563eb', '#7c3aed', '#0891b2', '#ea580c', '#16a34a', '#64748b', '#db2777',
];

function Frame({ children, height, testId }) {
  return (
    <div data-testid={testId} style={{ width: '100%', height }}>
      <ResponsiveContainer width="100%" height="100%">
        {children}
      </ResponsiveContainer>
    </div>
  );
}

export function SimpleBarChart({ data, xKey, yKey, height = 280, testId = 'bar-chart' }) {
  if (!data?.length) return <EmptyState title="No data to chart" icon="📊" />;
  return (
    <Frame height={height} testId={testId}>
      <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis dataKey={xKey} tick={{ fontSize: 12 }} />
        <YAxis tick={{ fontSize: 12 }} />
        <Tooltip />
        <Bar dataKey={yKey} fill={CHART_PALETTE[0]} radius={[4, 4, 0, 0]} />
      </BarChart>
    </Frame>
  );
}

export function SimpleLineChart({ data, xKey, yKey, height = 280, testId = 'line-chart' }) {
  if (!data?.length) return <EmptyState title="No data to chart" icon="📈" />;
  return (
    <Frame height={height} testId={testId}>
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis dataKey={xKey} tick={{ fontSize: 12 }} />
        <YAxis tick={{ fontSize: 12 }} />
        <Tooltip />
        <Line type="monotone" dataKey={yKey} stroke={CHART_PALETTE[0]} dot={false} strokeWidth={2} />
      </LineChart>
    </Frame>
  );
}

export function SimplePieChart({ data, nameKey, valueKey, height = 280, testId = 'pie-chart' }) {
  if (!data?.length) return <EmptyState title="No data to chart" icon="🥧" />;
  return (
    <Frame height={height} testId={testId}>
      <PieChart>
        <Pie data={data} dataKey={valueKey} nameKey={nameKey} outerRadius={100} label>
          {data.map((_, i) => <Cell key={i} fill={CHART_PALETTE[i % CHART_PALETTE.length]} />)}
        </Pie>
        <Tooltip />
        <Legend />
      </PieChart>
    </Frame>
  );
}
