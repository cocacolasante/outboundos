import client from './client.js';

/** The whitelist (objects -> fields with type / operators / aggregates). */
export async function getReportMetadata() {
  const { data } = await client.get('/reports/metadata');
  return data;
}

export async function listReports() {
  const { data } = await client.get('/reports');
  return data;
}

export async function getReport(id) {
  const { data } = await client.get(`/reports/${id}`);
  return data;
}

export async function createReport(payload) {
  const { data } = await client.post('/reports', payload);
  return data;
}

export async function updateReport(id, payload) {
  const { data } = await client.patch(`/reports/${id}`, payload);
  return data;
}

export async function duplicateReport(id) {
  const { data } = await client.post(`/reports/${id}/duplicate`);
  return data;
}

export async function deleteReport(id) {
  await client.delete(`/reports/${id}`);
}

/** Run an unsaved definition (builder preview). */
export async function runAdhocReport(payload) {
  const { data } = await client.post('/reports/run', payload);
  return data;
}

/** Run a saved report by id. */
export async function runSavedReport(id) {
  const { data } = await client.post(`/reports/${id}/run`);
  return data;
}
