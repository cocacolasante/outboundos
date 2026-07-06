import client from './client.js';

// ---------- Watches ----------

export async function listWatches() {
  const { data } = await client.get('/signals/watches');
  return data;
}

export async function createWatch(payload) {
  const { data } = await client.post('/signals/watches', payload);
  return data;
}

export async function updateWatch(id, payload) {
  const { data } = await client.patch(`/signals/watches/${id}`, payload);
  return data;
}

export async function deleteWatch(id) {
  await client.delete(`/signals/watches/${id}`);
}

export async function runWatchNow(id) {
  const { data } = await client.post(`/signals/watches/${id}/run-now`);
  return data;
}

// ---------- Signal feed ----------

export async function listSignals(params = {}) {
  const { data } = await client.get('/signals', { params });
  return data;
}

export async function actionSignal(id) {
  const { data } = await client.post(`/signals/${id}/action`);
  return data;
}

export async function dismissSignal(id) {
  const { data } = await client.post(`/signals/${id}/dismiss`);
  return data;
}

/** Light contact lookup (Hunter-first). On a hit, stages + links a
 *  campaign-less lead so the signal becomes contactable. */
export async function enrichSignalContact(id) {
  const { data } = await client.post(`/signals/${id}/enrich`);
  return data;
}

/** One watch per company (funding/hiring) — paste a list, get
 *  {created, skipped_duplicate, watch_ids}. */
export async function createWatchesBulk(payload) {
  const { data } = await client.post('/signals/watches/bulk', payload);
  return data;
}

// ---------- Nonprofit funding discovery feeds (Settings → Discovery) ----------

export async function listFundingSources() {
  const { data } = await client.get('/signals/funding/sources');
  return data;
}

export async function updateFundingSource(source, payload) {
  const { data } = await client.patch(`/signals/funding/sources/${source}`, payload);
  return data;
}

export async function runFundingSourceNow(source) {
  const { data } = await client.post(`/signals/funding/sources/${source}/run-now`);
  return data;
}

export async function stopFundingSource(source) {
  const { data } = await client.post(`/signals/funding/sources/${source}/stop`);
  return data;
}

// ---------- Signal outreach: draft → send (logs CRM lead + activity) ----------

export async function draftSignalEmail(id, payload = {}) {
  const { data } = await client.post(`/signals/${id}/draft`, payload);
  return data;
}

export async function sendSignalEmail(id, payload) {
  const { data } = await client.post(`/signals/${id}/send`, payload);
  return data;
}

/** Bulk-add the staged leads behind selected signals into a campaign;
 *  marks those signals actioned. Returns {added, skipped_*, signals_actioned}. */
export async function addSignalsToCampaign(payload) {
  const { data } = await client.post('/signals/add-to-campaign', payload);
  return data;
}
