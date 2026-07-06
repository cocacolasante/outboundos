import client from './client.js';

export async function listAccounts() {
  const { data } = await client.get('/connected-accounts/');
  return data;
}

export async function getAccount(id) {
  const { data } = await client.get(`/connected-accounts/${id}`);
  return data;
}

export async function createAccount(payload) {
  const { data } = await client.post('/connected-accounts/', payload);
  return data;
}

export async function updateAccount(id, payload) {
  const { data } = await client.patch(`/connected-accounts/${id}`, payload);
  return data;
}

export async function deleteAccount(id) {
  await client.delete(`/connected-accounts/${id}`);
}

export async function testAccount(id) {
  const { data } = await client.post(`/connected-accounts/${id}/test`);
  return data;
}

export async function getAccountStatus(id) {
  const { data } = await client.get(`/connected-accounts/${id}/status`);
  return data;
}
