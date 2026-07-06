import client from './client.js';

// ---------- Agent settings (runtime singleton) ----------

export async function getAgentSettings() {
  const { data } = await client.get('/agent/settings');
  return data;
}

export async function updateAgentSettings(payload) {
  const { data } = await client.patch('/agent/settings', payload);
  return data;
}

// ---------- Notifications (the bell) ----------

export async function listNotifications(params = {}) {
  const { data } = await client.get('/agent/notifications', { params });
  return data;
}

export async function markNotificationRead(id) {
  const { data } = await client.post(`/agent/notifications/${id}/read`);
  return data;
}

export async function markAllNotificationsRead() {
  const { data } = await client.post('/agent/notifications/read-all');
  return data;
}

// ---------- Audit log ----------

export async function listAgentActions(params = {}) {
  const { data } = await client.get('/agent/actions', { params });
  return data;
}

// ---------- Reply triage feed ----------

export async function listReplies(params = {}) {
  const { data } = await client.get('/agent/replies', { params });
  return data;
}
