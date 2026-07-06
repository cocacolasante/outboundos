import client from './client.js';

// ---------- Leads (manual creation + CRM status) ----------

export async function createCrmLead(payload) {
  const { data } = await client.post('/crm/leads', payload);
  return data;
}

export async function updateLeadCrmStatus(leadId, crmStatus) {
  const { data } = await client.patch(`/crm/leads/${leadId}`, { crm_status: crmStatus });
  return data;
}

/** Update a lead's contact info / details (email, name, company,
 *  job_title, phone, linkedin_url, company_website, notes). PATCH —
 *  only the keys you pass are changed. */
export async function updateLeadFields(leadId, payload) {
  const { data } = await client.patch(`/crm/leads/${leadId}`, payload);
  return data;
}

/** Convert a lead → opportunity. Carries the contact snapshot; flips
 *  the lead to crm_status=converted. 409 when already converted. */
export async function convertLead(leadId, payload = {}) {
  const { data } = await client.post(`/crm/leads/${leadId}/convert`, payload);
  return data;
}

// ---------- Opportunities ----------

export async function listOpportunities(params = {}) {
  const { data } = await client.get('/crm/opportunities', { params });
  return data;
}

export async function getOpportunity(id) {
  const { data } = await client.get(`/crm/opportunities/${id}`);
  return data;
}

export async function createOpportunity(payload) {
  const { data } = await client.post('/crm/opportunities', payload);
  return data;
}

export async function updateOpportunity(id, payload) {
  const { data } = await client.patch(`/crm/opportunities/${id}`, payload);
  return data;
}

export async function deleteOpportunity(id) {
  await client.delete(`/crm/opportunities/${id}`);
}

/** Per-stage roll-up {stage, count, total_amount} for the Kanban header. */
export async function getPipelineSummary() {
  const { data } = await client.get('/crm/opportunities/pipeline');
  return data;
}

/** The default pipeline + its configurable, ordered stages (Kanban columns). */
export async function getDefaultPipeline() {
  const { data } = await client.get('/crm/pipelines/default');
  return data;
}

/** Append-only stage-move audit for one opportunity (newest first). */
export async function getStageHistory(oppId) {
  const { data } = await client.get(`/crm/opportunities/${oppId}/stage-history`);
  return data;
}

// ---------- Activities ----------

export async function listActivities(params = {}) {
  const { data } = await client.get('/crm/activities', { params });
  return data;
}

export async function createActivity(payload) {
  const { data } = await client.post('/crm/activities', payload);
  return data;
}

export async function updateActivity(id, payload) {
  const { data } = await client.patch(`/crm/activities/${id}`, payload);
  return data;
}

export async function deleteActivity(id) {
  await client.delete(`/crm/activities/${id}`);
}


// ---------- Documents ----------

export async function listDocuments(oppId) {
  const { data } = await client.get(`/crm/opportunities/${oppId}/documents`);
  return data;
}

export async function uploadDocument(oppId, file) {
  const form = new FormData();
  form.append('file', file);
  const { data } = await client.post(
    `/crm/opportunities/${oppId}/documents`, form,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  );
  return data;
}

/** Returns the raw download URL — used as an <a href> so the browser
 *  handles the attachment natively. */
export function documentDownloadUrl(docId) {
  const base = client.defaults.baseURL || '';
  return `${base.replace(/\/$/, '')}/crm/documents/${docId}/download`;
}

export async function deleteDocument(docId) {
  await client.delete(`/crm/documents/${docId}`);
}

// ---------- Products of interest ----------

export async function listProducts(oppId) {
  const { data } = await client.get(`/crm/opportunities/${oppId}/products`);
  return data;
}

export async function addProduct(oppId, payload) {
  const { data } = await client.post(`/crm/opportunities/${oppId}/products`, payload);
  return data;
}

export async function updateProduct(productId, payload) {
  const { data } = await client.patch(`/crm/products/${productId}`, payload);
  return data;
}

export async function deleteProduct(productId) {
  await client.delete(`/crm/products/${productId}`);
}
