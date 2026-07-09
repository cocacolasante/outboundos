import client from './client.js';

export async function getMe() {
  const { data } = await client.get('/auth/me');
  return data;
}

export async function login({ email, password }) {
  const { data } = await client.post('/auth/login', { email, password });
  return data;
}

export async function register({ email, password, tenantName, inviteCode }) {
  const { data } = await client.post('/auth/register', {
    email,
    password,
    tenant_name: tenantName || null,
    invite_code: inviteCode || null,
  });
  return data;
}

export async function logout() {
  const { data } = await client.post('/auth/logout');
  return data;
}

export async function forgotPassword(email) {
  const { data } = await client.post('/auth/forgot', { email });
  return data;
}

export async function resetPassword({ token, password }) {
  const { data } = await client.post('/auth/reset', { token, password });
  return data;
}

export async function acceptInvite({ token, password }) {
  const { data } = await client.post('/auth/accept-invite', { token, password });
  return data;
}

// --- Team / workspace (multi-tenancy Phase 6) ---

export async function listTeam() {
  const { data } = await client.get('/auth/team');
  return data;
}

export async function inviteMember({ email, role }) {
  const { data } = await client.post('/auth/team/invite', { email, role });
  return data;
}

export async function removeMember(membershipId) {
  await client.delete(`/auth/team/${membershipId}`);
}

export async function renameTenant(name) {
  const { data } = await client.patch('/auth/tenant', { name });
  return data;
}
