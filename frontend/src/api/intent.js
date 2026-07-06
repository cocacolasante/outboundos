import client from './client.js';

// ---------- Engine dashboard + run controls ----------

export async function getIntentStatus() {
  const { data } = await client.get('/intent/status');
  return data;
}

export async function seedIntentOrgs(payload) {
  const { data } = await client.post('/intent/orgs/seed', payload);
  return data;
}

export async function runIntentCollectors() {
  const { data } = await client.post('/intent/collectors/run');
  return data;
}

export async function recomputeIntent() {
  const { data } = await client.post('/intent/recompute');
  return data;
}

export async function promoteIntent() {
  const { data } = await client.post('/intent/promote');
  return data;
}

// ---------- ICP profiles ----------

export async function listIntentProfiles() {
  const { data } = await client.get('/intent/profiles');
  return data;
}

export async function createIntentPreset(kind) {
  const { data } = await client.post(`/intent/profiles/preset?kind=${kind}`);
  return data;
}

export async function updateIntentProfile(id, payload) {
  const { data } = await client.patch(`/intent/profiles/${id}`, payload);
  return data;
}

export async function activateIntentProfile(id) {
  const { data } = await client.post(`/intent/profiles/${id}/activate`);
  return data;
}

export async function deleteIntentProfile(id) {
  await client.delete(`/intent/profiles/${id}`);
}

export async function getRankedIntent(id, limit = 50) {
  const { data } = await client.get(`/intent/profiles/${id}/intent?limit=${limit}`);
  return data;
}
