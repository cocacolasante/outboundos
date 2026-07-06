import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { researchClient, sendClientEmail } from '../api/researchClient.js';
import { listAccounts as listConnectedAccounts } from '../api/connectedAccounts.js';
import { signatureToPreviewHtml } from '../utils/signaturePreview.js';

const SENDER_NAME_KEY = 'researchClient.senderName';

const DEFAULT_LIMIT = {
  linkedin_dm: 300,
  email: 600,
};

const TONE_PRESETS = ['professional', 'warm', 'direct', 'casual'];


function CharCountBadge({ count, limit }) {
  const over = count > limit;
  return (
    <span
      data-testid="char-count"
      className={`text-xs font-medium ${over ? 'text-red-600' : 'text-slate-500'}`}
    >
      {count} / {limit} chars
    </span>
  );
}


function CopyButton({ text, label = 'Copy' }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  async function onCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Browser denied clipboard access; nothing graceful to do.
    }
  }
  return (
    <button
      type="button"
      onClick={onCopy}
      className="px-3 py-1.5 text-sm font-medium bg-slate-100 hover:bg-slate-200 rounded-md text-slate-700"
    >
      {copied ? 'Copied!' : label}
    </button>
  );
}


// Very loose email check — enough to disable the Send button while the
// user is typing.  Real validation happens server-side via Pydantic's
// EmailStr, which catches the cases this regex misses.
const _EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;


function EmailActionPanel({ initialSubject, initialBody, senderName, profile }) {
  // 4 states: ``idle`` (Add email button), ``editing`` (form open),
  // ``sending`` (in-flight), ``sent`` (confirmation shown).  We
  // intentionally don't persist this state — the panel resets when the
  // user re-runs research / changes inputs.
  const [stage, setStage] = useState('idle');
  const [toEmail, setToEmail] = useState('');
  const [subject, setSubject] = useState(initialSubject || '');
  const [body, setBody] = useState(initialBody || '');
  // Empty string = "use the configured default" — backend falls back to
  // settings.BREVO_SENDER_EMAIL and the payload omits sender_email.
  // Any other value = the connected-account email_address chosen below.
  const [fromEmail, setFromEmail] = useState('');
  const [sentTo, setSentTo] = useState(null);
  const [sentAt, setSentAt] = useState(null);
  const [crmNote, setCrmNote] = useState(null);

  // Connected accounts power the From-address picker.  Fetched once at
  // render — these change rarely.  Empty array on error or no accounts;
  // in that case the picker hides itself and the send uses the configured
  // default automatically.
  const { data: connectedAccounts } = useQuery({
    queryKey: ['connected-accounts'],
    queryFn: listConnectedAccounts,
    staleTime: 60_000,
  });
  const accounts = Array.isArray(connectedAccounts) ? connectedAccounts : [];

  // Resolve which account's signature will be applied server-side so we
  // can show it as a non-editable preview.  Mirrors the backend's
  // priority chain: explicit picker selection > workspace default
  // sender > nothing.  ``fromEmail`` is the picker state ('' = default
  // option).
  const resolvedAccount = useMemo(() => {
    if (fromEmail) {
      return accounts.find((a) => a.email_address === fromEmail) || null;
    }
    return accounts.find((a) => a.is_default_sender) || null;
  }, [fromEmail, accounts]);
  const signaturePreview = (resolvedAccount?.signature || '').trim();

  const recipientName = useMemo(() => {
    const parts = [profile?.first_name, profile?.last_name]
      .map((s) => (s || '').trim())
      .filter(Boolean);
    return parts.join(' ') || null;
  }, [profile]);

  // Keep the editable copies in sync when the parent regenerates the
  // research (which produces a new subject/body).  Skipped while editing
  // so we don't clobber the user's typing.
  useEffect(() => {
    if (stage === 'idle') {
      setSubject(initialSubject || '');
      setBody(initialBody || '');
    }
  }, [initialSubject, initialBody, stage]);

  const mut = useMutation({
    mutationFn: () => sendClientEmail({
      to_email: toEmail.trim(),
      to_name: recipientName,
      subject: subject,
      body: body,
      sender_name: senderName || 'Sender',
      // Omit sender_email when "configured default" is selected so the
      // server uses settings.BREVO_SENDER_EMAIL.  Picking a connected
      // account passes its email_address through as an override.
      ...(fromEmail ? { sender_email: fromEmail } : {}),
    }),
    onSuccess: (data) => {
      setSentTo(data.to_email);
      setSentAt(new Date(data.sent_at));
      // CRM auto-tracking summary for the confirmation row.
      if (data.crm_activity_logged) {
        setCrmNote(
          data.crm_lead_created
            ? 'Added to CRM as a new lead + email logged.'
            : 'Email logged on their existing CRM record.',
        );
      } else {
        setCrmNote(null);
      }
      setStage('sent');
    },
  });

  const canSend =
    _EMAIL_RE.test(toEmail.trim()) &&
    subject.trim().length > 0 &&
    body.trim().length > 0 &&
    !mut.isPending;

  const errDetail = mut.error?.response?.data?.detail || (mut.error && String(mut.error.message));

  if (stage === 'sent') {
    return (
      <div
        data-testid="email-send-success"
        className="rounded-md bg-emerald-50 border border-emerald-200 p-3 text-sm"
      >
        <span className="font-medium text-emerald-800">
          ✓ Sent to {sentTo}
        </span>
        <span className="text-emerald-700 ml-2">
          at {sentAt?.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>
        {crmNote && (
          <span className="text-emerald-600 ml-2 text-xs" data-testid="crm-tracking-note">
            · {crmNote}
          </span>
        )}
        <button
          type="button"
          onClick={() => {
            // Reset to idle but preserve the (possibly-edited) subject/body
            // so the user can fire another send to a different recipient
            // without redoing their edits.
            setStage('idle');
            setToEmail('');
            mut.reset();
          }}
          className="ml-3 text-xs text-emerald-700 hover:text-emerald-900 underline"
        >
          Send to someone else
        </button>
      </div>
    );
  }

  if (stage === 'idle') {
    return (
      <button
        type="button"
        onClick={() => setStage('editing')}
        data-testid="add-email-btn"
        className="px-3 py-1.5 text-sm font-medium bg-brand-600 hover:bg-brand-700 text-white rounded-md"
      >
        + Add email
      </button>
    );
  }

  // stage === 'editing'
  return (
    <div
      data-testid="email-action-form"
      className="bg-slate-50 border border-slate-200 rounded-md p-4 space-y-3"
    >
      {accounts.length > 0 && (
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">
            Send from
          </label>
          <select
            value={fromEmail}
            onChange={(e) => setFromEmail(e.target.value)}
            data-testid="send-from-picker"
            className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white"
          >
            <option value="">Use configured default sender</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.email_address}>
                {a.label ? `${a.label} — ${a.email_address}` : a.email_address}
              </option>
            ))}
          </select>
          {fromEmail && (
            <p className="text-xs text-amber-700 mt-1">
              Note: this address must be a verified sender on your Brevo
              account, otherwise the send will be rejected.
            </p>
          )}
        </div>
      )}
      <div>
        <label className="block text-xs font-semibold text-slate-600 mb-1">
          Send to (email address)
        </label>
        <input
          type="email"
          value={toEmail}
          onChange={(e) => setToEmail(e.target.value)}
          placeholder="jane@example.com"
          data-testid="send-to-email"
          autoFocus
          className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white"
        />
      </div>
      <div>
        <label className="block text-xs font-semibold text-slate-600 mb-1">
          Subject (edit if needed)
        </label>
        <input
          type="text"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          data-testid="send-subject"
          className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white"
        />
      </div>
      <div>
        <label className="block text-xs font-semibold text-slate-600 mb-1">
          Body (edit if needed)
        </label>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={10}
          data-testid="send-body"
          className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white font-mono leading-relaxed"
        />
        {signaturePreview && (
          <div
            data-testid="signature-preview"
            className="mt-2 rounded-md border border-dashed border-slate-300 bg-slate-50 p-3"
          >
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-semibold text-slate-600">
                Signature appended on send
              </span>
              <span className="text-xs text-slate-400">
                from {resolvedAccount?.label || resolvedAccount?.email_address}
              </span>
            </div>
            {/* Render the signature HTML inline so the user sees what the
                recipient will actually see — links, images, formatting all
                live.  Trusted source (the user typed it in their own
                Settings).  ``signature-preview-text`` is kept as a
                test-id alias so existing tests keep matching. */}
            <div
              data-testid="signature-preview-text"
              className="text-sm text-slate-700"
              // signatureToPreviewHtml mirrors the backend renderer:
              // newlines only become <br> OUTSIDE tags, plain text is
              // escaped, bare links get the default style — so the
              // preview matches the actually-sent email.
              dangerouslySetInnerHTML={{
                __html: signatureToPreviewHtml(signaturePreview),
              }}
            />
            <p className="text-xs text-slate-500 mt-2">
              Edit the signature in Settings → Connected inboxes if you want to change it.
            </p>
          </div>
        )}
      </div>
      {errDetail && (
        <div
          data-testid="send-error"
          className="text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2"
        >
          {errDetail}
        </div>
      )}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => mut.mutate()}
          disabled={!canSend}
          data-testid="send-email-btn"
          className="px-4 py-2 text-sm font-medium bg-brand-600 hover:bg-brand-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white rounded-md"
        >
          {mut.isPending ? 'Sending…' : 'Send email'}
        </button>
        <button
          type="button"
          onClick={() => {
            // Snap back to idle; keep subject/body edits in case the user
            // re-opens the form.
            setStage('idle');
            mut.reset();
          }}
          className="px-4 py-2 text-sm font-medium bg-white border border-slate-300 hover:bg-slate-50 text-slate-700 rounded-md"
        >
          Cancel
        </button>
        <span className="text-xs text-slate-500 ml-auto">
          Sends via Brevo from your configured sender address.
        </span>
      </div>
    </div>
  );
}


function ResultPanel({ result, outputKind, charLimit, senderName }) {
  if (!result) return null;
  const { profile, research, subject, body, char_count, duration_ms } = result;
  const fullText = subject ? `Subject: ${subject}\n\n${body}` : body;

  return (
    <div data-testid="result-panel" className="space-y-5">
      <div className="bg-slate-50 border border-slate-200 rounded-lg p-4">
        <div className="flex items-baseline justify-between mb-2">
          <h3 className="text-base font-semibold text-slate-800">
            {profile.first_name} {profile.last_name || ''}
          </h3>
          <span
            className={`text-xs px-2 py-0.5 rounded-full font-medium ${
              profile.quality === 'rich'
                ? 'bg-emerald-100 text-emerald-700'
                : profile.quality === 'partial'
                ? 'bg-amber-100 text-amber-700'
                : 'bg-slate-200 text-slate-600'
            }`}
          >
            {profile.quality} research
          </span>
        </div>
        <p className="text-sm text-slate-600">
          {[profile.job_title, profile.company].filter(Boolean).join(' · ') || '—'}
        </p>
        {profile.headline && (
          <p className="text-xs text-slate-500 mt-1">{profile.headline}</p>
        )}
        {!profile.found && (
          <p className="text-xs text-amber-700 mt-2">
            Web research didn't return high-confidence identity signals. The
            generated message uses the URL slug as a name guess and skips
            personalization.
          </p>
        )}
      </div>

      {subject && (
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">
            {outputKind === 'email' ? 'Subject' : 'Subject (LinkedIn thread topic)'}
          </label>
          <div className="flex items-center gap-2">
            <input
              type="text"
              readOnly
              value={subject}
              data-testid="result-subject"
              className="flex-1 px-3 py-2 border border-slate-300 rounded-md text-sm bg-white"
            />
            <CopyButton text={subject} />
          </div>
        </div>
      )}

      <div>
        <div className="flex items-baseline justify-between mb-1">
          <label className="block text-xs font-semibold text-slate-600">
            {outputKind === 'email' ? 'Body' : 'Message'}
          </label>
          <CharCountBadge count={char_count} limit={charLimit} />
        </div>
        <textarea
          readOnly
          value={body}
          rows={outputKind === 'email' ? 10 : 6}
          data-testid="result-body"
          className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm bg-white font-mono leading-relaxed"
        />
        <div className="mt-2 flex items-center gap-2">
          <CopyButton text={body} label="Copy message" />
          <CopyButton text={fullText} label="Copy with subject" />
          <span className="text-xs text-slate-400 ml-auto">
            generated in {(duration_ms / 1000).toFixed(1)}s
          </span>
        </div>
        {/* Send-now flow is email-only — LinkedIn DMs go out via Unipile
            (manual paste for now) and don't have a Brevo path. */}
        {outputKind === 'email' && (
          <div className="mt-3">
            <EmailActionPanel
              initialSubject={subject}
              initialBody={body}
              senderName={senderName}
              profile={profile}
            />
          </div>
        )}
      </div>

      <details className="text-sm text-slate-600">
        <summary className="cursor-pointer text-xs font-semibold text-slate-500 uppercase tracking-wider">
          Research details
        </summary>
        <div className="mt-3 space-y-2 pl-2 border-l-2 border-slate-200">
          {research.person_news?.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-slate-500">Person news</div>
              <ul className="list-disc list-inside text-xs">
                {research.person_news.map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            </div>
          )}
          {research.company_news?.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-slate-500">Company news</div>
              <ul className="list-disc list-inside text-xs">
                {research.company_news.map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            </div>
          )}
          {research.company_description && (
            <div>
              <div className="text-xs font-semibold text-slate-500">Company</div>
              <p className="text-xs">{research.company_description}</p>
            </div>
          )}
          {research.industry && (
            <p className="text-xs"><span className="font-semibold">Industry:</span> {research.industry}</p>
          )}
          {research.company_website && (
            <p className="text-xs"><span className="font-semibold">Website:</span> {research.company_website}</p>
          )}
        </div>
      </details>
    </div>
  );
}


export default function ResearchClient() {
  const [linkedinUrl, setLinkedinUrl] = useState('');
  const [goal, setGoal] = useState('');
  const [tone, setTone] = useState('professional');
  const [senderName, setSenderName] = useState('');
  const [researchMode, setResearchMode] = useState('fast');
  const [outputKind, setOutputKind] = useState('linkedin_dm');
  const [charLimit, setCharLimit] = useState(DEFAULT_LIMIT.linkedin_dm);
  const [touchedLimit, setTouchedLimit] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  // Restore sender name from prior session.
  useEffect(() => {
    try {
      const saved = localStorage.getItem(SENDER_NAME_KEY);
      if (saved) setSenderName(saved);
    } catch { /* noop */ }
  }, []);

  // Auto-pick a sensible char limit when the output kind changes, unless
  // the user has explicitly overridden it.
  useEffect(() => {
    if (!touchedLimit) {
      setCharLimit(DEFAULT_LIMIT[outputKind]);
    }
  }, [outputKind, touchedLimit]);

  const mutation = useMutation({
    mutationFn: researchClient,
    onSuccess: () => {
      try {
        if (senderName) localStorage.setItem(SENDER_NAME_KEY, senderName);
      } catch { /* noop */ }
    },
  });

  // Drive the elapsed-time counter while the request is in flight.
  useEffect(() => {
    if (!mutation.isPending) {
      setElapsed(0);
      return undefined;
    }
    const started = Date.now();
    const tick = setInterval(() => setElapsed((Date.now() - started) / 1000), 250);
    return () => clearInterval(tick);
  }, [mutation.isPending]);

  const expectedSeconds = researchMode === 'deep' ? 45 : 10;
  const error = mutation.error;
  const errorMessage = useMemo(() => {
    if (!error) return null;
    const detail = error?.response?.data?.detail;
    if (typeof detail === 'string') return detail;
    return String(error.message || 'Request failed');
  }, [error]);

  function onSubmit(e) {
    e.preventDefault();
    if (!linkedinUrl.trim() || !goal.trim()) return;
    mutation.mutate({
      linkedin_url: linkedinUrl.trim(),
      goal: goal.trim(),
      tone: tone.trim() || 'professional',
      sender_name: senderName.trim(),
      research_mode: researchMode,
      output_kind: outputKind,
      char_limit: Number(charLimit) || DEFAULT_LIMIT[outputKind],
    });
  }

  return (
    <div className="max-w-3xl mx-auto px-6 py-8">
      <header className="mb-6">
        <h1 className="text-2xl font-bold text-slate-900">Research a client</h1>
        <p className="text-sm text-slate-500 mt-1">
          One-off outreach: paste a LinkedIn URL, describe the goal, and get a
          personalized message you can send manually. No ghost-view
          notifications — research uses public web search only.
        </p>
      </header>

      <form onSubmit={onSubmit} className="space-y-4 bg-white p-6 rounded-lg border border-slate-200">
        <div>
          <label className="block text-sm font-semibold text-slate-700 mb-1">
            LinkedIn URL <span className="text-red-500">*</span>
          </label>
          <input
            type="url"
            placeholder="https://www.linkedin.com/in/their-slug/"
            value={linkedinUrl}
            onChange={(e) => setLinkedinUrl(e.target.value)}
            required
            data-testid="input-linkedin-url"
            className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
          />
        </div>

        <div>
          <label className="block text-sm font-semibold text-slate-700 mb-1">
            Goal of outreach <span className="text-red-500">*</span>
          </label>
          <textarea
            placeholder="e.g. Book a 15-min intro call about scaling onboarding for B2B SaaS"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            required
            rows={3}
            data-testid="input-goal"
            className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Research depth
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setResearchMode('fast')}
                data-testid="mode-fast"
                aria-pressed={researchMode === 'fast'}
                className={`flex-1 px-3 py-2 text-sm rounded-md border ${
                  researchMode === 'fast'
                    ? 'bg-slate-900 text-white border-slate-900'
                    : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
                }`}
              >
                Quick (~10s)
              </button>
              <button
                type="button"
                onClick={() => setResearchMode('deep')}
                data-testid="mode-deep"
                aria-pressed={researchMode === 'deep'}
                className={`flex-1 px-3 py-2 text-sm rounded-md border ${
                  researchMode === 'deep'
                    ? 'bg-slate-900 text-white border-slate-900'
                    : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
                }`}
              >
                Deep (~45s)
              </button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Output
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setOutputKind('linkedin_dm')}
                data-testid="output-dm"
                aria-pressed={outputKind === 'linkedin_dm'}
                className={`flex-1 px-3 py-2 text-sm rounded-md border ${
                  outputKind === 'linkedin_dm'
                    ? 'bg-slate-900 text-white border-slate-900'
                    : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
                }`}
              >
                LinkedIn DM
              </button>
              <button
                type="button"
                onClick={() => setOutputKind('email')}
                data-testid="output-email"
                aria-pressed={outputKind === 'email'}
                className={`flex-1 px-3 py-2 text-sm rounded-md border ${
                  outputKind === 'email'
                    ? 'bg-slate-900 text-white border-slate-900'
                    : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
                }`}
              >
                Email
              </button>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Character limit
            </label>
            <input
              type="number"
              min={50}
              max={5000}
              value={charLimit}
              onChange={(e) => {
                setTouchedLimit(true);
                setCharLimit(e.target.value);
              }}
              data-testid="input-char-limit"
              className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
            />
            <p className="text-xs text-slate-400 mt-1">
              Default {DEFAULT_LIMIT[outputKind]} for {outputKind === 'email' ? 'email' : 'DM'}
            </p>
          </div>
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Tone
            </label>
            <input
              type="text"
              value={tone}
              onChange={(e) => setTone(e.target.value)}
              list="tone-presets"
              data-testid="input-tone"
              className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
            />
            <datalist id="tone-presets">
              {TONE_PRESETS.map((t) => <option key={t} value={t} />)}
            </datalist>
          </div>
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Sender name
            </label>
            <input
              type="text"
              value={senderName}
              onChange={(e) => setSenderName(e.target.value)}
              placeholder="Your name (optional)"
              data-testid="input-sender-name"
              className="w-full px-3 py-2 border border-slate-300 rounded-md text-sm"
            />
          </div>
        </div>

        <div className="flex items-center justify-between pt-2">
          {mutation.isPending ? (
            <p className="text-sm text-slate-600">
              Researching… <span className="font-mono">{elapsed.toFixed(1)}s</span>
              <span className="text-slate-400"> / ~{expectedSeconds}s</span>
            </p>
          ) : <span />}
          <button
            type="submit"
            disabled={mutation.isPending || !linkedinUrl.trim() || !goal.trim()}
            data-testid="submit-research"
            className="px-5 py-2 bg-slate-900 text-white text-sm font-semibold rounded-md hover:bg-slate-800 disabled:bg-slate-300 disabled:cursor-not-allowed"
          >
            {mutation.isPending ? 'Working…' : 'Generate outreach'}
          </button>
        </div>

        {errorMessage && !mutation.isPending && (
          <p data-testid="error-message" className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-md p-3">
            {errorMessage}
          </p>
        )}
      </form>

      {mutation.data && (
        <div className="mt-8 bg-white p-6 rounded-lg border border-slate-200">
          <ResultPanel
            result={mutation.data}
            outputKind={outputKind}
            charLimit={Number(charLimit) || DEFAULT_LIMIT[outputKind]}
            senderName={senderName}
          />
        </div>
      )}
    </div>
  );
}
