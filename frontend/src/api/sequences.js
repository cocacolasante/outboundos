import client from './client.js';

export async function getSequence(campaignId) {
  const { data } = await client.get(`/campaigns/${campaignId}/sequence`);
  return data;
}

export async function updateSequence(campaignId, payload) {
  const { data } = await client.put(`/campaigns/${campaignId}/sequence`, payload);
  return data;
}

export async function validateSequence(campaignId) {
  const { data } = await client.post(`/campaigns/${campaignId}/sequence/validate`);
  return data;
}

export async function publishSequence(campaignId) {
  const { data } = await client.post(`/campaigns/${campaignId}/sequence/publish`);
  return data;
}

export async function getSequenceAnalytics(campaignId) {
  const { data } = await client.get(`/campaigns/${campaignId}/sequence/analytics`);
  return data;
}
