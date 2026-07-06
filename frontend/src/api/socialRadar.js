import client from './client.js';

// ---------- Searches ----------

export async function listSearches(params = {}) {
  const { data } = await client.get('/social-radar/searches', { params });
  return data;
}

export async function getSearch(id) {
  const { data } = await client.get(`/social-radar/searches/${id}`);
  return data;
}

export async function createSearch(payload) {
  const { data } = await client.post('/social-radar/searches', payload);
  return data;
}

export async function updateSearch(id, payload) {
  const { data } = await client.patch(`/social-radar/searches/${id}`, payload);
  return data;
}

export async function deleteSearch(id) {
  await client.delete(`/social-radar/searches/${id}`);
}

export async function runSearch(id) {
  const { data } = await client.post(`/social-radar/searches/${id}/run`);
  return data;
}

/** Delete posts older than the search's max_post_age_days (or with no
 *  post_date at all).  Cascades to opportunities. Returns {deleted: N}. */
export async function cleanupStalePosts(id) {
  const { data } = await client.post(`/social-radar/searches/${id}/cleanup-stale`);
  return data;
}

/** Forecast Anthropic spend for the NEXT Run-now invocation. Used to
 *  label the Run button so the user sees cost before clicking. */
export async function estimateRunCost(id) {
  const { data } = await client.get(`/social-radar/searches/${id}/estimate`);
  return data;
}

/** Re-score every existing post for this search under the current
 *  qualifier prompt. Preserves user-set status/notes. ~$0.10/100 posts. */
export async function requalifyAll(id) {
  const { data } = await client.post(`/social-radar/searches/${id}/requalify-all`);
  return data;
}

/** Sync Anthropic call to preview the expanded queries before saving the search. */
export async function previewExpand(payload) {
  const { data } = await client.post('/social-radar/expand-preview', payload);
  return data;
}

/** Same preview, but for an already-saved search (uses its current settings). */
export async function previewExpandForSearch(id) {
  const { data } = await client.post(`/social-radar/searches/${id}/expand-preview`);
  return data;
}

// ---------- Opportunities ----------

export async function listOpportunities(params = {}) {
  const { data } = await client.get('/social-radar/opportunities', { params });
  return data;
}

export async function updateOpportunity(id, payload) {
  const { data } = await client.patch(`/social-radar/opportunities/${id}`, payload);
  return data;
}
