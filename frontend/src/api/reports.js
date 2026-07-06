import client from './client.js';

/** Date-scoped CRM report dashboard: KPIs, won/lost trend, pipeline +
 *  forecast snapshots, loss reasons, activity breakdown, funnel.
 *  params: { start?: 'YYYY-MM-DD', end?: 'YYYY-MM-DD' } */
export async function getReportOverview(params = {}) {
  const { data } = await client.get('/crm/reports/overview', { params });
  return data;
}

/** Deal detail rows for a table + CSV export.
 *  params: { outcome: 'won'|'lost'|'open'|'all', start?, end? } */
export async function getReportDeals(params = {}) {
  const { data } = await client.get('/crm/reports/deals', { params });
  return data;
}

/** Logged-activity detail rows.
 *  params: { start?, end?, activity_type? } */
export async function getReportActivities(params = {}) {
  const { data } = await client.get('/crm/reports/activities', { params });
  return data;
}
