import client from './client.js';

export async function listTenants() {
  const { data } = await client.get('/admin/tenants');
  return data;
}

export async function overridePlan(tenantId, { plan, subscription_status }) {
  const { data } = await client.post(`/admin/tenants/${tenantId}/plan`, {
    plan,
    subscription_status,
  });
  return data;
}

export async function grantFreeAccess(tenantId) {
  const { data } = await client.post(`/admin/tenants/${tenantId}/grant-free`);
  return data;
}

export async function cancelAccess(tenantId) {
  const { data } = await client.post(`/admin/tenants/${tenantId}/cancel`);
  return data;
}

export async function deleteTenant(tenantId) {
  const { data } = await client.delete(`/admin/tenants/${tenantId}`);
  return data;
}

export async function impersonateTenant(tenantId) {
  const { data } = await client.post(`/admin/tenants/${tenantId}/impersonate`);
  return data;
}

export async function getStats() {
  const { data } = await client.get('/admin/stats');
  return data;
}

export async function listInviteLinks() {
  const { data } = await client.get('/admin/invite-links');
  return data;
}

export async function createInviteLink({ label, trial_days, max_uses }) {
  const { data } = await client.post('/admin/invite-links', {
    label,
    trial_days,
    max_uses: max_uses || null,
  });
  return data;
}

export async function revokeInviteLink(linkId) {
  const { data } = await client.post(`/admin/invite-links/${linkId}/revoke`);
  return data;
}

export async function listAudit(limit = 100) {
  const { data } = await client.get('/admin/audit', { params: { limit } });
  return data;
}
