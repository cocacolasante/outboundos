import client from './client.js';

export async function getApiStatus() {
  const { data } = await client.get('/settings/api-status');
  return data;
}

// --- BYOK integrations (multi-tenancy Phase 4) ---

export async function listIntegrations() {
  const { data } = await client.get('/settings/integrations');
  return data;
}

export async function saveIntegration(provider, payload) {
  const { data } = await client.put(`/settings/integrations/${provider}`, payload);
  return data;
}

export async function testIntegration(provider) {
  const { data } = await client.post(`/settings/integrations/${provider}/test`);
  return data;
}

export async function deleteIntegration(provider) {
  await client.delete(`/settings/integrations/${provider}`);
}

export async function registerUnipileWebhooks() {
  const { data } = await client.post('/settings/integrations/unipile/register-webhooks');
  return data;
}
