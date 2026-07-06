import client from './client.js';

export async function getIcpProfile() {
  const { data } = await client.get('/icp/profile');
  return data;
}

export async function regenerateIcpProfile() {
  const { data } = await client.post('/icp/profile/regenerate');
  return data;
}

export async function updateIcpCriteria(criteria) {
  const { data } = await client.patch('/icp/profile', { criteria });
  return data;
}

export async function listCandidates(params = {}) {
  const { data } = await client.get('/icp/candidates', { params });
  return data;
}

export async function acceptCandidate(id) {
  const { data } = await client.post(`/icp/candidates/${id}/accept`);
  return data;
}

export async function rejectCandidate(id) {
  const { data } = await client.post(`/icp/candidates/${id}/reject`);
  return data;
}
