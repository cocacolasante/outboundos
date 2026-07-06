/**
 * /ui-kit — internal design-system preview (Phase 2, UI refinement).
 * Renders every primitive in its variants/states so the system can be
 * eyeballed together. Presentation-only; not linked from the product nav.
 */
import { useState } from 'react';

import {
  Button, Card, PageHeader,
  Input, Textarea, Select, Field, Checkbox, Radio, Toggle,
  Modal, Badge, Tabs, Tooltip, Menu, Table,
} from '../components/ui.jsx';
import { Skeleton, LoadingCards, EmptyState, ErrorState } from '../components/states.jsx';
import { SEMANTIC } from '../utils/statusColors.js';

function Section({ title, children }) {
  return (
    <section className="mb-10">
      <h2 className="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-3">{title}</h2>
      <Card className="p-5 space-y-4">{children}</Card>
    </section>
  );
}

const ROWS = [
  { id: 1, name: 'Acme Corp', stage: 'Proposal', amount: 24000 },
  { id: 2, name: 'Beeco', stage: 'Negotiation', amount: 8000 },
  { id: 3, name: 'Cee Industries', stage: 'Won', amount: 51000 },
];

export default function UiKit() {
  const [modal, setModal] = useState(false);
  const [tab, setTab] = useState('one');
  const [toggle, setToggle] = useState(true);
  const [sort, setSort] = useState({ key: 'amount', dir: 'desc' });
  const [err, setErr] = useState(false);

  const sorted = [...ROWS].sort((a, b) => {
    const d = a[sort.key] > b[sort.key] ? 1 : -1;
    return sort.dir === 'asc' ? d : -d;
  });

  return (
    <div className="p-8 max-w-4xl mx-auto" data-testid="ui-kit">
      <PageHeader title="UI kit" subtitle="Design-system primitives — every variant + state." />

      <Section title="Buttons">
        <div className="flex flex-wrap items-center gap-3">
          <Button>Primary</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="danger">Danger</Button>
          <Button variant="ghost">Ghost</Button>
          <Button size="sm">Small</Button>
          <Button loading>Loading</Button>
          <Button disabled>Disabled</Button>
        </div>
      </Section>

      <Section title="Form controls">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Email" hint="We never share it.">
            {(p) => <Input placeholder="you@co.com" {...p} />}
          </Field>
          <Field label="Amount" required error="Required field">
            {(p) => <Input type="number" placeholder="0" {...p} />}
          </Field>
          <Field label="Stage" optional>
            {(p) => <Select {...p}><option>Prospecting</option><option>Proposal</option></Select>}
          </Field>
          <Field label="Notes">
            {(p) => <Textarea rows={2} placeholder="…" {...p} />}
          </Field>
        </div>
        <div className="flex flex-wrap items-center gap-6 pt-1">
          <Checkbox label="Subscribe" defaultChecked />
          <Radio name="r" label="Option A" defaultChecked />
          <Radio name="r" label="Option B" />
          <label className="inline-flex items-center gap-2 text-sm text-slate-700">
            <Toggle checked={toggle} onChange={setToggle} label="Notifications" /> Notifications
          </label>
        </div>
      </Section>

      <Section title="Badges">
        <div className="flex flex-wrap gap-2">
          {Object.keys(SEMANTIC).map((v) => <Badge key={v} variant={v} dot>{v}</Badge>)}
        </div>
      </Section>

      <Section title="Tabs">
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[{ key: 'one', label: 'Overview' }, { key: 'two', label: 'Activity', count: 3 }, { key: 'three', label: 'Settings' }]}
        />
        <p className="text-sm text-slate-600 pt-2">Active: {tab}</p>
      </Section>

      <Section title="Overlays">
        <div className="flex items-center gap-3">
          <Button onClick={() => setModal(true)} data-testid="open-modal">Open modal</Button>
          <Menu label="Actions" items={[
            { label: 'Edit', onSelect: () => {} },
            { label: 'Duplicate', onSelect: () => {} },
            { label: 'Delete', danger: true, onSelect: () => {} },
          ]} />
          <Tooltip label="A helpful hint">
            <Button variant="secondary">Hover me</Button>
          </Tooltip>
        </div>
        <Modal open={modal} onClose={() => setModal(false)} title="Example modal"
          footer={<>
            <Button variant="secondary" size="sm" onClick={() => setModal(false)}>Cancel</Button>
            <Button size="sm" onClick={() => setModal(false)}>Confirm</Button>
          </>}>
          <p className="text-sm text-slate-600 m-0">
            Spring enter/exit, focus-trapped, Esc + backdrop close, aria-modal.
          </p>
        </Modal>
      </Section>

      <Section title="Table">
        <Table
          testId="kit-table"
          columns={[
            { key: 'name', label: 'Name', sortable: true },
            { key: 'stage', label: 'Stage' },
            { key: 'amount', label: 'Amount', numeric: true, sortable: true, render: (r) => `$${r.amount.toLocaleString()}` },
          ]}
          rows={sorted}
          sort={sort}
          onSort={(key) => setSort((s) => ({ key, dir: s.key === key && s.dir === 'asc' ? 'desc' : 'asc' }))}
        />
      </Section>

      <Section title="States">
        <div className="space-y-4">
          <div className="flex gap-2"><Skeleton className="h-4 w-40" /><Skeleton className="h-4 w-24" /></div>
          <LoadingCards count={2} />
          <EmptyState title="Nothing here yet" hint="A clear next action goes here." />
          {err
            ? <ErrorState message="Something failed." onRetry={() => setErr(false)} />
            : <Button variant="secondary" size="sm" onClick={() => setErr(true)}>Show error state</Button>}
        </div>
      </Section>
    </div>
  );
}
