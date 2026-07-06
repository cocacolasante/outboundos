import client from './client.js';

export async function getBilling() {
  const { data } = await client.get('/billing');
  return data;
}

export async function createCheckout(plan) {
  const { data } = await client.post('/billing/checkout', { plan });
  return data; // { url }
}

export async function createPortal() {
  const { data } = await client.post('/billing/portal');
  return data; // { url }
}
