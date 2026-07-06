import client from './client.js';

export async function listLinkedInAccounts() {
  const { data } = await client.get('/linkedin-accounts/');
  return data;
}

export async function getLinkedInAccount(id) {
  const { data } = await client.get(`/linkedin-accounts/${id}`);
  return data;
}

export async function updateLinkedInAccount(id, payload) {
  const { data } = await client.patch(`/linkedin-accounts/${id}`, payload);
  return data;
}

export async function deleteLinkedInAccount(id) {
  await client.delete(`/linkedin-accounts/${id}`);
}

export async function testLinkedInAccount(id) {
  const { data } = await client.post(`/linkedin-accounts/${id}/test`);
  return data;
}

export async function resolveLinkedInChallenge(id) {
  const { data } = await client.post(`/linkedin-accounts/${id}/resolve-challenge`, {});
  return data;
}

// --- Unipile hosted-auth flow ----------------------------------------
// 1. POST /connect-via-unipile -> { account_id, hosted_url }
// 2. Frontend opens hosted_url in a new tab so the user logs in to LinkedIn
//    through Unipile's hosted form.
// 3. Unipile fires webhook -> backend persists unipile_account_id + status.
// 4. Frontend polls /sync-unipile (or just /:id) until status flips to OK.

export async function connectViaUnipile(payload) {
  const { data } = await client.post('/linkedin-accounts/connect-via-unipile', payload);
  return data;
}

export async function syncUnipileStatus(id) {
  const { data } = await client.post(`/linkedin-accounts/${id}/sync-unipile`);
  return data;
}

// --- Import existing Unipile accounts ---------------------------------
// When the user connected LinkedIn via Unipile's dashboard (not our portal),
// the account is in Unipile but not in our DB. `listDiscoverableUnipileAccounts`
// surfaces those; `importFromUnipile` binds one to a fresh local row.

export async function listDiscoverableUnipileAccounts() {
  const { data } = await client.get('/linkedin-accounts/discoverable');
  return data;
}

export async function importFromUnipile(payload) {
  const { data } = await client.post('/linkedin-accounts/import-from-unipile', payload);
  return data;
}
