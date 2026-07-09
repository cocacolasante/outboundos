import { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';

const NAV = [
  { id: 'getting-started', label: 'Getting Started', children: [
    { id: 'overview', label: 'Platform Overview' },
    { id: 'quick-start', label: 'Quick Start Guide' },
    { id: 'plans', label: 'Plans & Pricing' },
  ]},
  { id: 'integrations', label: 'Integrations', children: [
    { id: 'anthropic', label: 'Anthropic (AI)' },
    { id: 'brevo', label: 'Brevo (Email)' },
    { id: 'apollo', label: 'Apollo (Enrichment)' },
    { id: 'hunter', label: 'Hunter (Email Finder)' },
    { id: 'linkedin-setup', label: 'LinkedIn (Unipile)' },
    { id: 'inbox', label: 'Connecting an Inbox' },
  ]},
  { id: 'campaigns', label: 'Campaigns', children: [
    { id: 'create-campaign', label: 'Creating a Campaign' },
    { id: 'research-modes', label: 'Research Modes' },
    { id: 'templates', label: 'Template Mode' },
    { id: 'sequence-builder', label: 'Sequence Builder' },
    { id: 'uploading-leads', label: 'Uploading Leads' },
    { id: 'preview-launch', label: 'Preview & Launch' },
    { id: 'managing-campaigns', label: 'Managing Campaigns' },
    { id: 'schedule-pacing', label: 'Schedule & Pacing' },
    { id: 'retargeting', label: 'Retargeting Engaged Leads' },
    { id: 'analytics', label: 'Campaign Analytics' },
  ]},
  { id: 'leads-crm', label: 'Leads & CRM', children: [
    { id: 'leads', label: 'Lead Management' },
    { id: 'suppressing', label: 'Suppressing a Lead' },
    { id: 'opportunities', label: 'Opportunities & Pipeline' },
    { id: 'activities', label: 'Activities & Tasks' },
    { id: 'replies', label: 'Reply Inbox' },
    { id: 'research-client', label: 'Research a Client' },
  ]},
  { id: 'reporting', label: 'Reporting', children: [
    { id: 'dashboard', label: 'Dashboard' },
    { id: 'reports', label: 'Sales Reports' },
    { id: 'report-builder', label: 'Custom Report Builder' },
  ]},
  { id: 'workspace', label: 'Workspace', children: [
    { id: 'team', label: 'Team Management' },
    { id: 'billing', label: 'Billing & Usage' },
    { id: 'agent', label: 'CRM Agent Settings' },
    { id: 'deliverability', label: 'Deliverability & Reputation' },
  ]},
];

function SideNav({ active, onNavigate }) {
  const [expanded, setExpanded] = useState(() => {
    for (const section of NAV) {
      if (section.children?.some(c => c.id === active)) return section.id;
    }
    return NAV[0].id;
  });

  useEffect(() => {
    for (const section of NAV) {
      if (section.children?.some(c => c.id === active)) {
        setExpanded(section.id);
        break;
      }
    }
  }, [active]);

  return (
    <nav className="w-64 min-w-64 border-r border-slate-200 bg-white overflow-y-auto h-screen sticky top-0">
      <div className="p-5 border-b border-slate-200">
        <a href="/" className="text-lg font-bold text-slate-900 no-underline hover:text-brand-600 transition-colors">OutboundOS</a>
        <p className="text-xs text-slate-500 mt-0.5">Documentation</p>
      </div>
      <div className="py-3">
        {NAV.map(section => (
          <div key={section.id} className="mb-1">
            <button
              onClick={() => setExpanded(expanded === section.id ? null : section.id)}
              className="w-full flex items-center justify-between px-5 py-2 text-sm font-semibold text-slate-700 hover:text-slate-900 hover:bg-slate-50 transition-colors"
            >
              {section.label}
              <svg className={`w-3.5 h-3.5 text-slate-400 transition-transform ${expanded === section.id ? 'rotate-90' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
            </button>
            {expanded === section.id && section.children && (
              <div className="ml-5 border-l border-slate-200">
                {section.children.map(child => (
                  <button
                    key={child.id}
                    onClick={() => onNavigate(child.id)}
                    className={`w-full text-left pl-4 pr-5 py-1.5 text-sm transition-colors ${
                      active === child.id
                        ? 'text-brand-600 font-medium border-l-2 border-brand-600 -ml-px'
                        : 'text-slate-500 hover:text-slate-900'
                    }`}
                  >
                    {child.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </nav>
  );
}

function Section({ id, title, children }) {
  return (
    <section id={id} className="mb-12 scroll-mt-8">
      <h2 className="text-2xl font-bold text-slate-900 mb-4 pb-2 border-b border-slate-200">{title}</h2>
      <div className="prose-slate max-w-none text-[15px] leading-relaxed text-slate-700 space-y-4">
        {children}
      </div>
    </section>
  );
}

function Step({ n, title, children }) {
  return (
    <div className="flex gap-3 mb-3">
      <span className="flex-shrink-0 w-7 h-7 rounded-full bg-brand-600 text-white text-sm font-bold flex items-center justify-center mt-0.5">{n}</span>
      <div>
        <p className="font-semibold text-slate-900 mb-1">{title}</p>
        <div className="text-slate-600">{children}</div>
      </div>
    </div>
  );
}

function Tip({ children }) {
  return (
    <div className="bg-blue-50 border border-blue-200 rounded-lg px-4 py-3 text-sm text-blue-800">
      <span className="font-semibold">Tip: </span>{children}
    </div>
  );
}

function Warning({ children }) {
  return (
    <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-sm text-amber-800">
      <span className="font-semibold">Warning: </span>{children}
    </div>
  );
}

function Table({ headers, rows }) {
  return (
    <div className="overflow-x-auto border border-slate-200 rounded-lg">
      <table className="w-full text-sm text-left">
        <thead className="bg-slate-50 text-xs uppercase text-slate-500 border-b border-slate-200">
          <tr>{headers.map((h, i) => <th key={i} className="px-4 py-2.5">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-slate-100 last:border-0">
              {row.map((cell, j) => <td key={j} className="px-4 py-2.5 text-slate-700">{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DocsContent({ section }) {
  switch (section) {
    case 'overview': return (
      <Section id="overview" title="Platform Overview">
        <p>OutboundOS is an AI-powered cold outreach platform that automates the entire process of researching prospects, composing personalized emails, and managing multi-channel sequences across email and LinkedIn. Every touchpoint is personalized using Claude AI, which researches each lead from public web data before writing outreach that feels hand-crafted.</p>
        <p><strong>Core capabilities:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>AI-Powered Personalization</strong> &mdash; Claude researches every lead and writes personalized emails based on their role, company news, and public profile.</li>
          <li><strong>Multi-Step Sequences</strong> &mdash; Build automated sequences with emails, follow-ups, delays, and LinkedIn actions using a visual drag-and-drop builder.</li>
          <li><strong>LinkedIn Outreach</strong> &mdash; Connect requests, DMs, profile views, post reactions, and comments integrated into sequences.</li>
          <li><strong>Smart Reply Detection</strong> &mdash; Inbound replies are automatically classified as positive, neutral, or negative so you focus on hot leads.</li>
          <li><strong>Built-in CRM</strong> &mdash; Leads, opportunities, pipeline boards, deal tracking, activities, documents, and reporting &mdash; no separate tool needed.</li>
          <li><strong>Custom Reports</strong> &mdash; Build custom reports with filters, grouping, aggregates, and charts. Export to CSV.</li>
        </ul>
        <p><strong>BYOK (Bring Your Own Keys):</strong> OutboundOS uses your own API keys for AI, email sending, and enrichment. Your data stays in your accounts, and you control costs directly with each provider.</p>
      </Section>
    );
    case 'quick-start': return (
      <Section id="quick-start" title="Quick Start Guide">
        <p>Get your first campaign running in 10 minutes:</p>
        <Step n={1} title="Set up your integrations">
          <p>Go to <strong>Settings &rarr; Integrations</strong> and add your API keys. At minimum, you need <strong>Anthropic</strong> (for AI research and email composition) and <strong>Brevo</strong> (for email sending). Click "Test" after adding each key to verify the connection.</p>
        </Step>
        <Step n={2} title="Connect an inbox">
          <p>Go to <strong>Settings &rarr; Connected inboxes</strong> and add your email account via IMAP. This enables reply tracking &mdash; when prospects respond, their replies appear in your Replies inbox and are automatically classified.</p>
        </Step>
        <Step n={3} title="Create a campaign">
          <p>Click <strong>+ New campaign</strong> from the Campaigns page. Enter your campaign name, outreach goal, sender details, and choose a research mode. The "Fast" research mode works well for most use cases.</p>
        </Step>
        <Step n={4} title="Build your sequence">
          <p>Use the visual sequence builder or click "Skip &mdash; use default" for a single-email campaign. Add wait nodes and follow-up emails for multi-step sequences.</p>
        </Step>
        <Step n={5} title="Upload your leads">
          <p>Upload a CSV file with your prospects. Required column: <strong>email</strong>. Recommended: first_name, last_name, company, job_title, linkedin_url.</p>
        </Step>
        <Step n={6} title="Preview and launch">
          <p>OutboundOS will research and compose sample emails. Review them, make edits if needed, then approve and launch. Emails are sent within your configured schedule window.</p>
        </Step>
        <Tip>Start with a small batch (20-50 leads) to test your campaign goal and review the AI output before scaling up.</Tip>
      </Section>
    );
    case 'plans': return (
      <Section id="plans" title="Plans & Pricing">
        <p>OutboundOS offers three plans. All new accounts start with a <strong>7-day free trial</strong> on the Starter plan.</p>
        <Table
          headers={['Feature', 'Starter ($20/mo)', 'Pro ($40/mo)', 'Agency ($100/mo)']}
          rows={[
            ['Emails per month', '1,000', '10,000', '50,000'],
            ['AI research calls', '1,000', '10,000', '50,000'],
            ['AI composes', '1,000', '10,000', '50,000'],
            ['LinkedIn actions', '200', '1,000', '5,000'],
            ['Active campaigns', '3', '10', 'Unlimited'],
            ['Connected inboxes', '1', '3', '10'],
            ['LinkedIn accounts', '1', '2', '10'],
            ['Team seats', '1', '3', '10'],
            ['LinkedIn outreach', '—', '✓', '✓'],
            ['Advanced analytics', '—', '✓', '✓'],
            ['Team collaboration', '—', '✓', '✓'],
            ['Multi-workspace', '—', '—', '✓'],
          ]}
        />
        <p>You can view current usage and change your plan anytime from <strong>Settings &rarr; Billing</strong>. Usage resets on the first of each calendar month.</p>
      </Section>
    );
    case 'anthropic': return (
      <Section id="anthropic" title="Anthropic (AI)">
        <p>The Anthropic API key powers all AI features: lead research, email composition, reply classification, and the Research-a-Client tool.</p>
        <Step n={1} title="Get your API key">
          <p>Sign up at <strong>console.anthropic.com</strong>, create a new API key under API Keys, and copy it.</p>
        </Step>
        <Step n={2} title="Add to OutboundOS">
          <p>Go to <strong>Settings &rarr; Integrations</strong>, find the Anthropic card, click "Add key", paste your API key, and click Save.</p>
        </Step>
        <Step n={3} title="Verify">
          <p>Click "Test" on the Anthropic card. You should see a green "Verified" badge.</p>
        </Step>
        <Tip>OutboundOS uses Claude Haiku for research (cost-efficient) and Claude Sonnet for email composition (quality). The platform automatically selects the right model for each task.</Tip>
      </Section>
    );
    case 'brevo': return (
      <Section id="brevo" title="Brevo (Email Sending)">
        <p>Brevo (formerly Sendinblue) handles all outbound email delivery and event tracking (opens, clicks, bounces, spam complaints).</p>
        <Step n={1} title="Create a Brevo account">
          <p>Sign up at <strong>brevo.com</strong>. The free tier includes 300 emails/day.</p>
        </Step>
        <Step n={2} title="Get your API key">
          <p>In Brevo, go to <strong>SMTP & API &rarr; API Keys</strong>, generate a new key, and copy it.</p>
        </Step>
        <Step n={3} title="Verify your sender address">
          <p>In Brevo, go to <strong>Senders, Domains & Dedicated IPs &rarr; Senders</strong>. Add and verify the email address you plan to send from. Your domain should have proper SPF, DKIM, and DMARC records configured.</p>
        </Step>
        <Step n={4} title="Add to OutboundOS">
          <p>Go to <strong>Settings &rarr; Integrations</strong>, find the Brevo card, enter your API key, default sender email, and sender name, then click Save.</p>
        </Step>
        <Step n={5} title="Verify">
          <p>Click "Test" on the Brevo card. You should see a green "Verified" badge.</p>
        </Step>
        <Warning>Make sure your sender email is verified in Brevo before launching campaigns. Unverified senders will cause send failures.</Warning>
      </Section>
    );
    case 'apollo': return (
      <Section id="apollo" title="Apollo (Lead Enrichment)">
        <p>Apollo enriches lead data with additional company and contact information. It is used when the "Deep" research mode is selected for a campaign.</p>
        <Step n={1} title="Get your API key">
          <p>Sign up at <strong>apollo.io</strong>, go to <strong>Settings &rarr; Integrations &rarr; API Access</strong>, and copy your API key.</p>
        </Step>
        <Step n={2} title="Add to OutboundOS">
          <p>Go to <strong>Settings &rarr; Integrations</strong>, find the Apollo card, click "Add key", paste your API key, and click Save.</p>
        </Step>
        <Tip>Apollo is optional. The "Fast" research mode uses web search only and does not require Apollo. Use "Deep" mode for higher-quality enrichment data when you need it.</Tip>
      </Section>
    );
    case 'hunter': return (
      <Section id="hunter" title="Hunter (Email Finder)">
        <p>Hunter.io finds professional email addresses by domain. It is used during contact enrichment and the Discovery feed workflows.</p>
        <Step n={1} title="Get your API key">
          <p>Sign up at <strong>hunter.io</strong>, go to your account dashboard, and copy your API key.</p>
        </Step>
        <Step n={2} title="Add to OutboundOS">
          <p>Go to <strong>Settings &rarr; Integrations</strong>, find the Hunter card, click "Add key", paste your API key, and click Save.</p>
        </Step>
        <Tip>Hunter is optional. Campaigns work without it. It is most useful when you have company domains but not individual email addresses.</Tip>
      </Section>
    );
    case 'linkedin-setup': return (
      <Section id="linkedin-setup" title="LinkedIn (Unipile)">
        <p>LinkedIn automation is powered by Unipile, a managed LinkedIn API that handles sessions on residential IPs. This enables connection requests, DMs, profile views, post reactions, and comments within your sequences.</p>
        <Step n={1} title="Get Unipile credentials">
          <p>Sign up at <strong>unipile.com</strong>. From your dashboard, copy your <strong>API key</strong> and <strong>DSN</strong> (e.g., <code>api12.unipile.com:13443</code>).</p>
        </Step>
        <Step n={2} title="Add to OutboundOS">
          <p>Go to <strong>Settings &rarr; Integrations</strong>, find the Unipile card, enter your API key and DSN, then click Save.</p>
        </Step>
        <Step n={3} title="Register webhooks">
          <p>Click the "Register webhooks" button on the Unipile card. This sets up real-time delivery of LinkedIn reply and connection-accept events.</p>
        </Step>
        <Step n={4} title="Connect a LinkedIn account">
          <p>Go to <strong>Settings &rarr; LinkedIn accounts</strong>, click "+ Connect LinkedIn account". This opens a Unipile-hosted login page where you authenticate with LinkedIn. Once connected, the account appears in your list.</p>
        </Step>
        <Step n={5} title="Test the connection">
          <p>Click "Test" on the account card. If LinkedIn requires a challenge (captcha/PIN), complete it in Unipile's hosted browser, then click "Clear challenge state" and re-test.</p>
        </Step>
        <Warning>LinkedIn automation carries risk. The system enforces conservative daily caps (20 connection requests, 30 DMs per account per day) and minimum delays between actions to protect your account.</Warning>
      </Section>
    );
    case 'inbox': return (
      <Section id="inbox" title="Connecting an Inbox">
        <p>Connecting an email inbox enables <strong>reply tracking</strong>. When prospects respond to your campaigns, their replies appear in the Replies inbox and are automatically classified by AI.</p>
        <Step n={1} title="Enable IMAP on your email account">
          <p>For Gmail: Settings &rarr; See all settings &rarr; Forwarding and POP/IMAP &rarr; Enable IMAP. For other providers, consult their documentation.</p>
        </Step>
        <Step n={2} title="Generate an app password">
          <p>If using Gmail with 2FA (recommended), generate an app password: Google Account &rarr; Security &rarr; 2-Step Verification &rarr; App passwords. Use this instead of your regular password.</p>
        </Step>
        <Step n={3} title="Connect in OutboundOS">
          <p>Go to <strong>Settings &rarr; Connected inboxes</strong>, click "+ Connect inbox", and fill in:</p>
          <ul className="list-disc pl-5 space-y-1 mt-2">
            <li><strong>Label</strong> &mdash; A name for this inbox (e.g., "Sales inbox")</li>
            <li><strong>Email address</strong> &mdash; The email you send from</li>
            <li><strong>IMAP host</strong> &mdash; e.g., <code>imap.gmail.com</code></li>
            <li><strong>IMAP port</strong> &mdash; Usually <code>993</code></li>
            <li><strong>Username</strong> &mdash; Usually your full email address</li>
            <li><strong>Password</strong> &mdash; Your app password</li>
            <li><strong>Signature</strong> &mdash; Optional HTML signature appended to one-off sends</li>
          </ul>
        </Step>
        <Step n={4} title="Test the connection">
          <p>Click "Test" on the inbox card. A green "Connected" badge confirms it is working.</p>
        </Step>
        <Tip>Set one inbox as your "Default sender" &mdash; this is used as the from-address for the Research-a-Client tool.</Tip>
      </Section>
    );
    case 'create-campaign': return (
      <Section id="create-campaign" title="Creating a Campaign">
        <p>Campaigns are the core workflow in OutboundOS. Each campaign targets a list of leads with a personalized outreach sequence.</p>
        <p>From the <strong>Campaigns</strong> page, click <strong>+ New campaign</strong> to start the 4-step creation wizard:</p>
        <Step n={1} title="Campaign Details">
          <p>Fill in the essential information:</p>
          <ul className="list-disc pl-5 space-y-1 mt-2">
            <li><strong>Name</strong> &mdash; Internal name for the campaign</li>
            <li><strong>Goal</strong> &mdash; What you want to achieve (e.g., "Book a 15-minute demo call"). The AI uses this to tailor every email.</li>
            <li><strong>Tone</strong> &mdash; Professional, Friendly, Direct, Conversational, or Formal</li>
            <li><strong>Sender name & email</strong> &mdash; The from-address on outgoing emails (must be verified in Brevo)</li>
            <li><strong>Research mode</strong> &mdash; Fast (web search, ~10s/lead), Deep (+Apollo, ~45s/lead), None (no research), or Template (manual copy)</li>
            <li><strong>Connected inbox</strong> &mdash; For reply tracking (optional but recommended)</li>
            <li><strong>LinkedIn account</strong> &mdash; Required only if your sequence includes LinkedIn steps</li>
            <li><strong>Schedule</strong> &mdash; Days of week, time window, timezone, and pacing limits</li>
          </ul>
        </Step>
        <Step n={2} title="Build Sequence">
          <p>Design your multi-step outreach sequence using the visual builder (see Sequence Builder section), or click "Skip &mdash; use default" for a single-email campaign.</p>
        </Step>
        <Step n={3} title="Upload Leads">
          <p>Upload a CSV with your prospect list. Required: <strong>email</strong>. Recommended: first_name, last_name, company.</p>
        </Step>
        <Step n={4} title="Prepare">
          <p>OutboundOS researches each lead and composes personalized emails. A progress bar shows the status. Once sample emails are ready, you advance to the preview screen.</p>
        </Step>
      </Section>
    );
    case 'research-modes': return (
      <Section id="research-modes" title="Research Modes">
        <p>The research mode controls how much information the AI gathers about each lead before writing their email.</p>
        <Table
          headers={['Mode', 'Speed', 'Cost', 'What happens']}
          rows={[
            ['Fast', '~10s/lead', '1 AI call + 1 web search', 'Claude searches the web for the lead\'s name, company, and role. Finds recent news, LinkedIn headline, and company context. Good for most use cases.'],
            ['Deep', '~45s/lead', '1 AI call + 1 web search + Apollo lookup', 'Everything in Fast, plus Apollo enrichment data (company size, industry, tech stack, etc). Best for high-value prospects.'],
            ['None', 'Instant', '1 AI call (no search)', 'AI writes from just the lead\'s name and company. No web research. Good when you already know your prospects or want speed over personalization.'],
            ['Template', 'Instant', 'Zero API calls', 'You write the email copy yourself using merge fields. No AI involved. Good for tested copy you want to send at scale.'],
          ]}
        />
        <Tip>The research cache lasts 90 days. If you run a second campaign targeting the same email addresses, research data is reused automatically &mdash; no extra cost.</Tip>
      </Section>
    );
    case 'templates': return (
      <Section id="templates" title="Template Mode">
        <p>Template mode lets you write your own email copy using merge fields instead of AI. This is useful when you have proven copy or need exact control over the message.</p>
        <p><strong>Available merge fields:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><code>{'{{first_name}}'}</code> &mdash; Lead's first name</li>
          <li><code>{'{{last_name}}'}</code> &mdash; Lead's last name</li>
          <li><code>{'{{company}}'}</code> &mdash; Lead's company</li>
          <li><code>{'{{job_title}}'}</code> &mdash; Lead's job title</li>
          <li><code>{'{{email}}'}</code> &mdash; Lead's email address</li>
          <li>Any CSV column header (e.g., <code>{'{{industry}}'}</code>, <code>{'{{city}}'}</code>)</li>
        </ul>
        <p><strong>Inline fallbacks:</strong> Use <code>{'{{first_name|there}}'}</code> to fall back to "there" if the first name is empty. For example: <code>Hi {'{{first_name|there}}'}</code> renders as "Hi Sarah" or "Hi there".</p>
      </Section>
    );
    case 'sequence-builder': return (
      <Section id="sequence-builder" title="Sequence Builder">
        <p>The sequence builder is a visual drag-and-drop canvas for designing multi-step outreach sequences. Each sequence is a graph of nodes (actions) connected by edges (transitions with optional conditions).</p>
        <p><strong>Available node types:</strong></p>
        <Table
          headers={['Node', 'Description']}
          rows={[
            ['Email', 'Send the AI-composed campaign email (entry node) or a follow-up with a custom template'],
            ['Reply', 'Reply in-thread to the lead\'s original email. Can be AI-composed or manual template.'],
            ['Wait', 'Pause for a set duration (minutes, hours, days, or weeks) before the next step'],
            ['LI: View profile', 'Ghost-view the lead\'s LinkedIn profile (low-touch warm-up)'],
            ['LI: Follow', 'Follow the lead on LinkedIn'],
            ['LI: React to post', 'Like, celebrate, or react to the lead\'s most recent post'],
            ['LI: Connect', 'Send a connection request, optionally with a personalized note (max 300 chars)'],
            ['LI: DM', 'Send a direct message (requires 1st-degree connection)'],
            ['LI: Comment', 'Comment on the lead\'s most recent post'],
          ]}
        />
        <p><strong>Edge conditions:</strong> Edges between nodes can have conditions that determine which path a lead follows:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Always</strong> &mdash; Lead always follows this path</li>
          <li><strong>Replied</strong> &mdash; Lead replied (within optional time window)</li>
          <li><strong>Opened</strong> &mdash; Lead opened the email</li>
          <li><strong>Clicked</strong> &mdash; Lead clicked a link</li>
          <li><strong>Bounced</strong> &mdash; Email bounced</li>
          <li><strong>LinkedIn connected</strong> &mdash; Connection request accepted/declined</li>
          <li><strong>Days since entered node</strong> &mdash; Lead has been on the current node for N+ days</li>
          <li><strong>And / Or / Not</strong> &mdash; Combine conditions with logic operators</li>
        </ul>
        <p><strong>Example sequence:</strong> Email &rarr; Wait 3 days &rarr; (if no reply) Follow-up email &rarr; Wait 2 days &rarr; (if no reply) LinkedIn connect &rarr; (if accepted) LinkedIn DM</p>
        <Tip>You can edit sequences on running campaigns. New steps apply to leads that haven't reached the end yet. Already-completed leads can be re-enrolled.</Tip>
      </Section>
    );
    case 'uploading-leads': return (
      <Section id="uploading-leads" title="Uploading Leads">
        <p>Leads are uploaded via CSV. The first row must contain column headers.</p>
        <p><strong>Required column:</strong> <code>email</code></p>
        <p><strong>Recommended columns:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><code>first_name</code> &mdash; Used in email personalization</li>
          <li><code>last_name</code> &mdash; Used in email personalization</li>
          <li><code>company</code> &mdash; Key research input for the AI</li>
          <li><code>job_title</code> &mdash; Provides role context for personalization</li>
          <li><code>linkedin_url</code> &mdash; Required for LinkedIn sequence steps</li>
          <li><code>phone</code> &mdash; Stored in the lead record for reference</li>
          <li><code>company_website</code> &mdash; Helps AI research if company name is ambiguous</li>
        </ul>
        <p>Any additional CSV columns are preserved and available as merge fields in template mode.</p>
        <p><strong>What happens during upload:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Duplicate emails within the same campaign are skipped</li>
          <li>Suppressed emails (unsubscribed, bounced, manually ignored) are skipped</li>
          <li>A summary shows how many leads were added, skipped, and suppressed</li>
        </ul>
      </Section>
    );
    case 'preview-launch': return (
      <Section id="preview-launch" title="Preview & Launch">
        <p>After leads are researched and emails are composed, you review a sample set before launching.</p>
        <p><strong>The preview screen shows:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Your campaign goal and tone at the top for context</li>
          <li>Each sample email with the lead's name, company, subject line, and full email body</li>
          <li>Approve/reject buttons for each individual email</li>
          <li>Inline editing of subject and body text</li>
        </ul>
        <p><strong>Actions:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Approve and launch</strong> &mdash; Approves all samples and starts sending within your schedule window</li>
          <li><strong>Reject and reconfigure</strong> &mdash; Rejects all and returns you to the campaign detail page to adjust the goal, tone, or other settings</li>
        </ul>
        <Tip>If the AI's output isn't quite right, try rewording your campaign goal. The goal is the single biggest input that shapes the AI's writing style and angle.</Tip>
      </Section>
    );
    case 'managing-campaigns': return (
      <Section id="managing-campaigns" title="Managing Campaigns">
        <p>Once a campaign is running, you can manage it from the campaign detail page:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Pause/Resume</strong> &mdash; Temporarily stop sending. Pausing does not lose your place; resume picks up where you left off.</li>
          <li><strong>Stop pipeline</strong> &mdash; Emergency stop that halts all in-flight research, composition, and sending. The campaign is paused.</li>
          <li><strong>Edit goal</strong> &mdash; Change the campaign goal. This triggers a rewrite of all unsent emails using existing research data (no new research cost). Already-sent emails are left as-is.</li>
          <li><strong>Add more leads</strong> &mdash; Upload additional leads to a running campaign. They enter the pipeline and are processed with the same settings.</li>
          <li><strong>Edit sequence</strong> &mdash; Modify the sequence on a live campaign. New steps apply to leads still in the sequence.</li>
          <li><strong>Apply signature</strong> &mdash; Set or change the email signature. Can be applied to all unsent composed emails.</li>
          <li><strong>Retry failed</strong> &mdash; Re-queue leads that failed during research, composition, or sending.</li>
        </ul>
        <p><strong>Campaign statuses:</strong></p>
        <Table
          headers={['Status', 'Meaning']}
          rows={[
            ['Draft', 'Campaign created but sequence not yet built or published'],
            ['Previewing', 'Leads uploaded, research/compose in progress or awaiting review'],
            ['Approved', 'Samples approved, ready to send'],
            ['Running', 'Actively sending emails within the schedule window'],
            ['Paused', 'Sending temporarily stopped (manually or by auto-pause)'],
            ['Complete', 'All leads processed and all sequence steps finished'],
          ]}
        />
        <p><strong>Auto-pause triggers:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>LinkedIn daily cap</strong> &mdash; Campaign auto-pauses when a LinkedIn account hits its daily action limit. Automatically resumes the next day.</li>
          <li><strong>Deliverability breaker</strong> &mdash; Campaign auto-pauses if the hard-bounce rate exceeds 5% or spam rate exceeds 0.1% over a 24-hour window (minimum 20 sends before triggering). This does NOT auto-resume &mdash; investigate the issue before resuming manually.</li>
        </ul>
      </Section>
    );
    case 'schedule-pacing': return (
      <Section id="schedule-pacing" title="Schedule & Pacing">
        <p>Every campaign has a send schedule that controls when emails go out:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Days</strong> &mdash; Which days of the week to send (default: Mon-Fri)</li>
          <li><strong>Time window</strong> &mdash; Start and end time (default: 9 AM - 5 PM)</li>
          <li><strong>Timezone</strong> &mdash; The timezone for the schedule window</li>
          <li><strong>Max per hour</strong> &mdash; Maximum sends per hour (optional)</li>
          <li><strong>Max per day</strong> &mdash; Maximum sends per day (optional)</li>
          <li><strong>Min delay</strong> &mdash; Minimum seconds between individual sends (default: 60)</li>
        </ul>
        <p><strong>Send-time optimization:</strong> When enabled, each email is deferred to the recipient's optimal local hour based on their past open behavior (or weekday mornings as a fallback), as long as it falls within the send window.</p>
        <p><strong>Domain-level protections:</strong> Shared caps apply across all campaigns using the same sending domain (100/hour, 500/day by default) to protect your domain reputation.</p>
        <Tip>Changing schedule or pacing settings takes effect immediately. Unsent leads are re-queued with the new schedule.</Tip>
      </Section>
    );
    case 'retargeting': return (
      <Section id="retargeting" title="Retargeting Engaged Leads">
        <p>Retargeting lets you create a follow-up campaign specifically for leads who engaged with a previous campaign.</p>
        <p><strong>Engaged leads include:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Leads who <strong>clicked a link</strong> in your email</li>
          <li>Leads who <strong>accepted a LinkedIn connection request</strong></li>
        </ul>
        <p><strong>How it works:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>From any campaign detail page, click <strong>"Retarget engaged"</strong></li>
          <li>The system shows how many engaged leads are available</li>
          <li>Choose to create a new campaign or add to an existing retarget campaign</li>
          <li>Duplicates are automatically prevented</li>
          <li>The AI references what the lead engaged with when composing the follow-up</li>
          <li>Research from the original campaign is reused (no extra cost)</li>
        </ul>
      </Section>
    );
    case 'analytics': return (
      <Section id="analytics" title="Campaign Analytics">
        <p>Each campaign has a dedicated analytics view showing performance metrics, sequence funnels, and reputation data.</p>
        <p><strong>Top-level metrics:</strong> Sent, Delivered, Open rate, Click rate, Reply rate, Bounce rate, Spam complaints, Unsubscribes</p>
        <p><strong>Timeline chart:</strong> Shows opens, clicks, and replies over the last 30 days.</p>
        <p><strong>Sequence performance:</strong> For multi-step sequences, a per-node breakdown shows sent/skipped/failed counts and how many leads are currently at each step.</p>
        <p><strong>Reputation card:</strong> A sender reputation score (0-100) based on delivery rate, spam rate, and bounce rate.</p>
        <p><strong>Research quality:</strong> Breakdown of leads by research quality (Rich, Partial, Generic) with open rates per bucket, so you can see if deeper research drives better engagement.</p>
        <p><strong>Best subject lines:</strong> Table of subject lines ranked by open rate.</p>
        <p><strong>"What's Working" AI insights:</strong> After replies are classified, the AI analyzes patterns and surfaces: winning openers, effective subject patterns, value framings that resonate, CTA styles, and approaches to avoid.</p>
      </Section>
    );
    case 'leads': return (
      <Section id="leads" title="Lead Management">
        <p>The <strong>Leads</strong> page shows every lead across all campaigns, plus manually-created CRM leads.</p>
        <p><strong>Features:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Search and filter</strong> &mdash; Search by name, email, or company. Filter by campaign or "has notes."</li>
          <li><strong>Bulk actions</strong> &mdash; Select multiple leads and add them to a different campaign.</li>
          <li><strong>Create CRM leads</strong> &mdash; Click "+ New lead" to manually create a lead that exists only in the CRM (not tied to any campaign).</li>
        </ul>
        <p><strong>Lead detail view</strong> (click any lead row):</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Contact info</strong> &mdash; Email, company, title, phone, LinkedIn, website, industry. All editable.</li>
          <li><strong>CRM status</strong> &mdash; New, Working, Qualified, Unqualified, or Converted</li>
          <li><strong>Engagement stats</strong> &mdash; Opens, clicks, replies, bounce status</li>
          <li><strong>Composed email</strong> &mdash; View the AI-written email for this lead</li>
          <li><strong>Research findings</strong> &mdash; Company description, person news, company news</li>
          <li><strong>Activity history</strong> &mdash; Timeline of all email events and sequence steps</li>
          <li><strong>Notes</strong> &mdash; Free-form notes per lead</li>
          <li><strong>Convert to opportunity</strong> &mdash; Creates a new deal in the pipeline</li>
          <li><strong>Track signals</strong> &mdash; Watch for job changes, funding, and hiring signals</li>
        </ul>
      </Section>
    );
    case 'suppressing': return (
      <Section id="suppressing" title="Suppressing a Lead">
        <p>Suppressing (ignoring) a lead prevents all future outreach to that email address across your entire workspace.</p>
        <p><strong>How to suppress a lead:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Open the lead detail view (click the lead row in the Leads page)</li>
          <li>Click the <strong>"Ignore lead"</strong> button</li>
          <li>Confirm the action</li>
        </ul>
        <p><strong>What happens when you suppress a lead:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>The email is added to the workspace-wide suppression list</li>
          <li>All active sequence steps for this lead across all campaigns are immediately halted</li>
          <li>Future campaigns will skip this email during lead upload</li>
          <li>The lead shows a red "Suppressed" badge in the UI</li>
        </ul>
        <p><strong>Automatic suppression:</strong> Leads are also automatically suppressed when:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>The prospect <strong>unsubscribes</strong> via the unsubscribe link</li>
          <li>The email <strong>hard bounces</strong></li>
          <li>The email is <strong>marked as spam</strong></li>
          <li>Brevo <strong>blocks</strong> the recipient</li>
          <li>The email <strong>soft bounces</strong> repeatedly (configurable threshold)</li>
        </ul>
        <p><strong>Un-suppressing:</strong> Click "Un-ignore" in the lead detail view. This removes the suppression, but does NOT reactivate halted sequence steps. The lead can be re-enrolled in new campaigns.</p>
        <Warning>Suppression is workspace-wide. If you suppress an email in one campaign, it is blocked from all current and future campaigns in your workspace.</Warning>
      </Section>
    );
    case 'opportunities': return (
      <Section id="opportunities" title="Opportunities & Pipeline">
        <p>The <strong>Opportunities</strong> page is a Kanban-style deal pipeline for managing your sales deals.</p>
        <p><strong>Pipeline stages:</strong> Prospecting &rarr; Qualification &rarr; Proposal &rarr; Negotiation &rarr; Closed Won / Closed Lost</p>
        <p><strong>Views:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Board view</strong> &mdash; Kanban board with drag-and-drop between stages. Each column shows the stage name, deal count, and total value.</li>
          <li><strong>List view</strong> &mdash; Table with columns: Name, Stage, Company, Amount, Close date.</li>
        </ul>
        <p><strong>Creating an opportunity:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>From the pipeline</strong> &mdash; Click "+ New opportunity" and fill in deal name, amount, expected close date, company, and contact email.</li>
          <li><strong>From a lead</strong> &mdash; Click "Convert to opportunity" in the lead detail view. The lead's data populates the new opportunity.</li>
          <li><strong>From a reply</strong> &mdash; Click "Convert" on a positive reply in the Replies inbox.</li>
        </ul>
        <p><strong>Opportunity detail page:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Stage stepper</strong> &mdash; Click any stage to move the deal. Closing as Won requires confirmation; closing as Lost requires a loss reason.</li>
          <li><strong>Deal details</strong> &mdash; Amount, close date, win probability (auto-set by stage, overridable), description.</li>
          <li><strong>Products of interest</strong> &mdash; Line items with product name, quantity, unit price.</li>
          <li><strong>Documents</strong> &mdash; Upload and manage proposals, contracts, and quotes (up to 10 MB each).</li>
          <li><strong>Activity log</strong> &mdash; Record calls, emails, meetings, notes, and tasks.</li>
        </ul>
      </Section>
    );
    case 'activities': return (
      <Section id="activities" title="Activities & Tasks">
        <p>Activities are manual touchpoint records you log on leads and opportunities. They help you track every interaction in one place.</p>
        <p><strong>Activity types:</strong> Call, Email (manual), Meeting, Note, Task</p>
        <p><strong>Each activity has:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Subject</strong> &mdash; Brief title</li>
          <li><strong>Body</strong> &mdash; Details or notes</li>
          <li><strong>Direction</strong> &mdash; Inbound or outbound</li>
          <li><strong>Due date</strong> &mdash; For tasks</li>
          <li><strong>Completed date</strong> &mdash; Mark tasks as done</li>
        </ul>
        <p>Activities appear in the timeline on both lead and opportunity detail views. The CRM agent can also automatically log activities (replies, suggested tasks) when enabled.</p>
      </Section>
    );
    case 'replies': return (
      <Section id="replies" title="Reply Inbox">
        <p>The <strong>Replies</strong> page shows all inbound replies that the AI agent has classified. This is your triage center for responding to prospects.</p>
        <p><strong>Each reply shows:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Sentiment badge</strong> &mdash; Positive (green), Neutral (gray), Negative (red)</li>
          <li><strong>Sender</strong> &mdash; Name, company, and email</li>
          <li><strong>Subject and body preview</strong></li>
          <li><strong>AI draft reply</strong> &mdash; Toggle to view an AI-generated suggested reply. This is never sent automatically &mdash; copy it and send manually.</li>
          <li><strong>Convert button</strong> &mdash; Quickly convert the lead to an opportunity if they're interested</li>
        </ul>
        <p><strong>Filtering:</strong> Filter by sentiment (All, Positive, Neutral, Negative) to focus on hot leads first.</p>
        <Tip>Enable "Email me on positive replies" in Settings &rarr; Agent to get notified immediately when a prospect shows interest.</Tip>
      </Section>
    );
    case 'research-client': return (
      <Section id="research-client" title="Research a Client">
        <p>The <strong>Research a Client</strong> tool is a one-off outreach composer. Paste a LinkedIn URL, set your goal, and get a personalized message in seconds. This uses public web search only &mdash; no LinkedIn ghost-view notifications.</p>
        <p><strong>Inputs:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>LinkedIn URL</strong> &mdash; The prospect's LinkedIn profile URL</li>
          <li><strong>Goal</strong> &mdash; What you want the outreach to achieve</li>
          <li><strong>Research depth</strong> &mdash; Quick (~10s) or Deep (~45s)</li>
          <li><strong>Output format</strong> &mdash; LinkedIn DM (300 chars) or Email (600 chars)</li>
          <li><strong>Tone</strong> &mdash; Professional, warm, direct, or casual</li>
          <li><strong>Sender name</strong> &mdash; Your name (saved across sessions)</li>
        </ul>
        <p><strong>Output:</strong> A personalized message with subject line (for email), ready to copy. You can also send the email directly from the tool if you have a connected inbox.</p>
        <p><strong>CRM integration:</strong> Sending an email from this tool automatically creates a CRM lead (if one doesn't exist) and logs the email as a CRM activity.</p>
      </Section>
    );
    case 'dashboard': return (
      <Section id="dashboard" title="Dashboard">
        <p>The <strong>Dashboard</strong> is your unified overview showing pipeline health, outreach performance, and saved reports.</p>
        <p><strong>Pipeline KPIs (last 90 days):</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Won this period (total revenue)</li>
          <li>Win rate</li>
          <li>Open pipeline value</li>
          <li>Weighted pipeline value</li>
          <li>Deals created, New leads, Conversions, Activities</li>
        </ul>
        <p><strong>Open pipeline by stage:</strong> A bar chart showing deal value per pipeline stage.</p>
        <p><strong>Outreach metrics:</strong> Total campaigns, emails sent, open rate, and reply rate across all campaigns.</p>
        <p><strong>Saved reports:</strong> Quick access to reports you have built with the Report Builder. Click "Run" to execute and see results inline.</p>
      </Section>
    );
    case 'reports': return (
      <Section id="reports" title="Sales Reports">
        <p>The <strong>Reports</strong> page provides comprehensive sales analytics with configurable date ranges.</p>
        <p><strong>Date range presets:</strong> Last 30 days, Last 90 days, Last 12 months, Year to date, All time, or custom dates.</p>
        <p><strong>Sections include:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>KPI cards</strong> &mdash; Won, Lost, Win rate, Avg deal size, Avg sales cycle, Open pipeline, New leads, Activities</li>
          <li><strong>Won vs Lost chart</strong> &mdash; Monthly comparison bar chart</li>
          <li><strong>Conversion funnel</strong> &mdash; New leads &rarr; Opportunities &rarr; Won, with conversion rates</li>
          <li><strong>Pipeline by stage</strong> &mdash; Deals, value, and weighted value per stage</li>
          <li><strong>Forecast by close month</strong> &mdash; Projected revenue by expected close date</li>
          <li><strong>Loss reasons</strong> &mdash; Why deals were lost, ranked by frequency and value</li>
          <li><strong>Activity breakdown</strong> &mdash; By type (call, email, meeting, note, task), direction, and human vs AI</li>
          <li><strong>Deal and activity detail tables</strong> &mdash; Exportable to CSV</li>
        </ul>
      </Section>
    );
    case 'report-builder': return (
      <Section id="report-builder" title="Custom Report Builder">
        <p>The <strong>Report Builder</strong> lets you create custom reports with filters, grouping, aggregates, and charts.</p>
        <p><strong>Building a report:</strong></p>
        <Step n={1} title="Choose a data source">
          <p>Select from available CRM objects (leads, opportunities, activities, etc).</p>
        </Step>
        <Step n={2} title="Select columns or grouping">
          <p>In <strong>Table mode</strong>, pick which fields to display as columns. In <strong>Summary mode</strong>, choose a group-by field and add aggregates (count, sum, avg, min, max).</p>
        </Step>
        <Step n={3} title="Add filters">
          <p>Add one or more filter conditions. Operators include: is, is not, contains, starts with, is any of, is empty, greater than, less than, between, before, after, relative range (last 30/60/90 days).</p>
        </Step>
        <Step n={4} title="Run and visualize">
          <p>Click "Run" to execute the report. For grouped reports, add a chart (Bar, Line, or Pie) to visualize the aggregates.</p>
        </Step>
        <p><strong>Saving:</strong> Save reports by name for quick access from the Dashboard. Duplicate a report to create variations. Export results to CSV.</p>
      </Section>
    );
    case 'team': return (
      <Section id="team" title="Team Management">
        <p>Manage your workspace team from <strong>Settings &rarr; Workspace</strong>.</p>
        <p><strong>Roles:</strong></p>
        <Table
          headers={['Role', 'Permissions']}
          rows={[
            ['Owner', 'Full access. Can rename workspace, invite/remove members, manage billing. Cannot be removed (only transferred).'],
            ['Admin', 'Same as Owner except cannot be the billing contact or transfer ownership.'],
            ['Member', 'Access to campaigns, leads, CRM, and reporting. Cannot manage team or billing.'],
          ]}
        />
        <p><strong>Inviting a team member:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Enter their email address and select a role (Admin or Member)</li>
          <li>They receive an email invitation with a link to set their password</li>
          <li>The seat is consumed at invite time (counts toward your plan's seat limit)</li>
          <li>Pending invites show a "Pending" badge until the invitee sets their password</li>
        </ul>
        <p><strong>Removing a member:</strong> Click the remove button next to their name. This revokes all their active sessions and removes their access immediately.</p>
      </Section>
    );
    case 'billing': return (
      <Section id="billing" title="Billing & Usage">
        <p>Manage your subscription and monitor usage from <strong>Settings &rarr; Billing</strong>.</p>
        <p><strong>What you see:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Current plan</strong> &mdash; Your active plan with subscription status</li>
          <li><strong>Trial countdown</strong> &mdash; Days remaining if on a free trial</li>
          <li><strong>Usage meters</strong> &mdash; Progress bars showing this month's usage vs. quota for: Emails sent, AI research, AI composes, LinkedIn actions</li>
          <li><strong>Change plan</strong> &mdash; Upgrade or downgrade between Starter, Pro, and Agency</li>
          <li><strong>Manage billing</strong> &mdash; Opens the Stripe customer portal where you can update payment methods, view invoices, and cancel</li>
        </ul>
        <p><strong>Usage resets:</strong> All monthly quotas reset on the 1st of each calendar month.</p>
        <p><strong>When you hit a limit:</strong> If a quota is exceeded, that specific action is blocked (e.g., no more emails sent) but the rest of the platform continues working. The system surfaces the reason clearly in the UI.</p>
        <Warning>If your trial expires without subscribing, sending is paused until you choose a plan. Your data is preserved.</Warning>
      </Section>
    );
    case 'agent': return (
      <Section id="agent" title="CRM Agent Settings">
        <p>The CRM agent automates classification, reminders, and notifications. Configure it from <strong>Settings &rarr; Agent</strong>.</p>
        <p><strong>What the agent does (and does NOT do):</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Logs inbound replies as CRM activities</li>
          <li>Classifies reply sentiment (positive/neutral/negative)</li>
          <li>Creates "Convert lead" task reminders on positive replies</li>
          <li>Drafts suggested reply text (never sent automatically)</li>
          <li>Nudges about stale deals (idle for 7+ days)</li>
          <li>Sends daily digest emails</li>
          <li><strong>Never</strong> sends emails to prospects, converts leads, or changes deal stages on its own</li>
        </ul>
        <p><strong>Toggles:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Auto-log inbound replies</li>
          <li>Convert reminders on positive replies</li>
          <li>Draft suggested replies</li>
          <li>Stale deal nudges</li>
          <li>Daily digest email</li>
          <li>Email on positive replies</li>
          <li>Email on every reply</li>
        </ul>
        <p><strong>Confidence threshold:</strong> A slider (0-100%) that sets the minimum classification confidence for the agent to act. Lower values catch more replies but may have false positives.</p>
        <p><strong>Quiet hours:</strong> Set a UTC time window during which alert emails are held until the daily digest.</p>
      </Section>
    );
    case 'deliverability': return (
      <Section id="deliverability" title="Deliverability & Reputation">
        <p>OutboundOS includes built-in deliverability protection to keep your domain reputation healthy.</p>
        <p><strong>Domain-level caps (shared across all campaigns on the same domain):</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>100 emails per hour per domain</li>
          <li>500 emails per day per domain</li>
        </ul>
        <p><strong>Circuit breaker (auto-pause):</strong> A campaign is automatically paused if, within a 24-hour window (minimum 20 sends):</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Hard-bounce rate exceeds <strong>5%</strong></li>
          <li>Spam complaint rate exceeds <strong>0.1%</strong></li>
        </ul>
        <p>This auto-pause does NOT auto-resume. Investigate the issue (bad list quality, content triggering spam filters) before resuming manually.</p>
        <p><strong>Best practices:</strong></p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Verify your sender domain with SPF, DKIM, and DMARC records</li>
          <li>Start with small batches and gradually increase volume</li>
          <li>Use a dedicated sending domain (not your primary business domain)</li>
          <li>Monitor the Reputation card in campaign Analytics</li>
          <li>Remove or suppress leads with invalid email addresses</li>
          <li>Keep bounce rate under 2% and spam rate under 0.05%</li>
        </ul>
      </Section>
    );
    default: return (
      <Section id="overview" title="Platform Overview">
        <p>Select a topic from the navigation to get started.</p>
      </Section>
    );
  }
}

export default function Docs() {
  const navigate = useNavigate();
  const location = useLocation();
  const hash = location.hash?.replace('#', '') || 'overview';
  const [section, setSection] = useState(hash);

  useEffect(() => {
    const newHash = location.hash?.replace('#', '');
    if (newHash && newHash !== section) {
      setSection(newHash);
    }
  }, [location.hash]);

  function handleNavigate(id) {
    setSection(id);
    navigate(`/docs#${id}`, { replace: true });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  return (
    <div className="flex min-h-screen bg-white">
      <SideNav active={section} onNavigate={handleNavigate} />
      <main className="flex-1 min-w-0 max-w-3xl mx-auto px-8 py-10">
        <DocsContent section={section} />
      </main>
    </div>
  );
}
