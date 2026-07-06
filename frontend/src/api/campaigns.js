import client from './client.js';

export async function listCampaigns() {
  const { data } = await client.get('/campaigns/');
  return data;
}

export async function getCampaign(id) {
  const { data } = await client.get(`/campaigns/${id}`);
  return data;
}

export async function createCampaign(payload) {
  const { data } = await client.post('/campaigns/', payload);
  return data;
}

export async function updateCampaign(id, payload) {
  const { data } = await client.patch(`/campaigns/${id}`, payload);
  return data;
}

export async function deleteCampaign(id) {
  await client.delete(`/campaigns/${id}`);
}

export async function pauseCampaign(id) {
  const { data } = await client.post(`/campaigns/${id}/pause`);
  return data;
}

export async function resumeCampaign(id) {
  const { data } = await client.post(`/campaigns/${id}/resume`);
  return data;
}

export async function stopCampaignPipeline(id) {
  // Hard-stop research + compose mid-flight.  Pauses the campaign,
  // revokes in-flight tasks, and raises a Redis flag so any
  // redelivered task bails before its next Anthropic call.
  const { data } = await client.post(`/campaigns/${id}/stop-pipeline`);
  return data;
}

export async function listCampaignLeads(id, params = {}) {
  const { data } = await client.get(`/campaigns/${id}/leads`, { params });
  return data;
}

// ---------- Preview ----------

export async function getPreview(id) {
  const { data } = await client.get(`/campaigns/${id}/preview`);
  return data;
}

export async function getPreviewProgress(id) {
  const { data } = await client.get(`/campaigns/${id}/preview/progress`);
  return data;
}

export async function updateSample(campaignId, leadId, payload) {
  const { data } = await client.patch(
    `/campaigns/${campaignId}/preview/samples/${leadId}`,
    payload,
  );
  return data;
}

export async function approveAll(campaignId) {
  const { data } = await client.post(`/campaigns/${campaignId}/preview/approve-all`);
  return data;
}

export async function rejectPreview(campaignId) {
  const { data } = await client.post(`/campaigns/${campaignId}/preview/reject`);
  return data;
}

// ---------- Lead upload ----------

export async function uploadLeadsPreview(campaignId, file) {
  const fd = new FormData();
  fd.append('file', file);
  const { data } = await client.post(
    `/campaigns/${campaignId}/upload`,
    fd,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  );
  return data;
}

export async function confirmLeadsUpload(campaignId, file, mapping) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mapping', JSON.stringify(mapping));
  const { data } = await client.post(
    `/campaigns/${campaignId}/leads/confirm-upload`,
    fd,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  );
  return data;
}

// ---------- Lead detail ----------

export async function deleteCampaignLead(campaignId, leadId) {
  await client.delete(`/campaigns/${campaignId}/leads/${leadId}`);
}

export async function getLeadDetail(campaignId, leadId) {
  const { data } = await client.get(`/campaigns/${campaignId}/leads/${leadId}`);
  return data;
}

export async function updateLeadEmail(campaignId, leadId, payload) {
  const { data } = await client.patch(`/campaigns/${campaignId}/leads/${leadId}`, payload);
  return data;
}

export async function findLeadContact(campaignId, leadId, payload = {}) {
  const { data } = await client.post(
    `/campaigns/${campaignId}/leads/${leadId}/find-contact`, payload,
  );
  return data;
}

export async function getRetargetPreview(campaignId) {
  const { data } = await client.get(`/campaigns/${campaignId}/retarget/preview`);
  return data;
}

export async function retargetCampaign(campaignId, payload = {}) {
  const { data } = await client.post(`/campaigns/${campaignId}/retarget`, payload);
  return data;
}

export async function previewLeadReply(campaignId, leadId, nodeId) {
  const { data } = await client.post(
    `/campaigns/${campaignId}/leads/${leadId}/reply-preview`,
    null,
    { params: nodeId ? { node_id: nodeId } : {} },
  );
  return data;
}

export async function listAllLeads(params = {}) {
  const { data } = await client.get(`/leads`, { params });
  return data;
}

/** Cross-campaign single-lead detail.  Returns LeadDetail with
 *  embedded activity timeline (sends + email events merged in time order),
 *  LinkedIn outreach state, and a research_summary block.  Used by the
 *  global Leads page CRM modal. */
export async function getLeadById(leadId) {
  const { data } = await client.get(`/leads/${leadId}`);
  return data;
}

/** Add the lead's email to the workspace suppression list AND halt any
 *  active sequence state rows for any lead carrying that email.  Future
 *  campaigns can't contact them; current ones stop progressing.
 *  Idempotent — returns {already_suppressed: true} on a re-ignore. */
export async function ignoreLead(leadId) {
  const { data } = await client.post(`/leads/${leadId}/ignore`);
  return data;
}

/** Reverse of ignoreLead — pulls the email off the suppression list.
 *  Does NOT reactivate the halted state rows; user has to re-enroll
 *  via the Activity tab if they want to contact the lead again. */
export async function unignoreLead(leadId) {
  await client.delete(`/leads/${leadId}/ignore`);
}

export async function applySignature(campaignId) {
  const { data } = await client.post(`/campaigns/${campaignId}/apply-signature`);
  return data;
}

// ---------- Activity ----------

export async function getCampaignActivity(id) {
  const { data } = await client.get(`/campaigns/${id}/activity`);
  return data;
}

export async function reEnrollHalted(campaignId) {
  const { data } = await client.post(`/campaigns/${campaignId}/re-enroll-halted`);
  return data;
}

// ---------- Analytics ----------

export async function getAnalytics(id) {
  const { data } = await client.get(`/campaigns/${id}/analytics`);
  return data;
}

// ---------- Errors / retry ----------

export async function listCampaignErrors(id) {
  const { data } = await client.get(`/campaigns/${id}/errors`);
  return data;
}

export async function retryFailedLeads(id) {
  const { data } = await client.post(`/campaigns/${id}/retry-failed`);
  return data;
}

/** Deliverability strip: window bounce/spam/open rates, sending-domain
 *  headroom, and breaker state. */
export async function getDeliverability(id) {
  const { data } = await client.get(`/campaigns/${id}/deliverability`);
  return data;
}

/** "What's working" panel: reply-outcome counts, cached winning-angle
 *  summary, and example winning messages. */
export async function getCopyInsights(id) {
  const { data } = await client.get(`/campaigns/${id}/copy-insights`);
  return data;
}

/** Bulk-add existing leads (CRM/manual/lookalike/another campaign's)
 *  into a campaign by COPY. Returns {added, skipped_*, research_started}. */
export async function addLeadsToCampaign(campaignId, leadIds) {
  const { data } = await client.post(`/campaigns/${campaignId}/leads/add`, {
    lead_ids: leadIds,
  });
  return data;
}
