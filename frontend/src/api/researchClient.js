import client from './client.js';

// 60s timeout — deep mode can take ~45s.  Default axios timeout (0 = no
// limit) is fine but being explicit prevents pathological hangs.
const ONE_MINUTE = 60_000;

export async function researchClient(payload) {
  const { data } = await client.post('/research-client', payload, {
    timeout: ONE_MINUTE,
  });
  return data;
}

/** One-off Brevo send for the Research-a-client tool. Backend builds the
 *  HTML/text bodies, calls Brevo with synthetic campaign/lead IDs, and
 *  returns {message_id, sent_at, to_email} on success.  502 on Brevo
 *  rejection (bad sender email, missing key, etc.) — caller renders the
 *  detail as a toast. */
export async function sendClientEmail(payload) {
  const { data } = await client.post('/research-client/send', payload);
  return data;
}
