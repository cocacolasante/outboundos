import { useNavigate } from 'react-router-dom';

const FEATURES = [
  {
    title: 'AI-Powered Personalization',
    desc: 'Claude researches every lead and writes personalized emails that feel hand-crafted — not templated.',
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
      </svg>
    ),
  },
  {
    title: 'Multi-Step Sequences',
    desc: 'Build automated sequences with emails, delays, and follow-ups. Set it and let OutboundOS handle the timing.',
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" />
      </svg>
    ),
  },
  {
    title: 'LinkedIn Outreach',
    desc: 'Connect requests, DMs, and profile views integrated into your sequences — all from one platform.',
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z" />
      </svg>
    ),
  },
  {
    title: 'Deep Lead Research',
    desc: 'AI automatically researches each prospect — their company, role, recent news — so every touchpoint is relevant.',
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
      </svg>
    ),
  },
  {
    title: 'Smart Reply Detection',
    desc: 'Replies are automatically classified — interested, not interested, out of office — so you focus on hot leads.',
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
      </svg>
    ),
  },
  {
    title: 'CRM & Pipeline',
    desc: 'Built-in CRM with opportunities, deal stages, and activity tracking. No need for a separate tool.',
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
      </svg>
    ),
  },
];

const PLANS = [
  {
    name: 'Starter',
    price: '$20',
    desc: 'For individuals getting started with outbound.',
    features: ['AI-composed emails', 'Multi-step sequences', 'Lead research', 'Reply detection', 'CRM basics'],
    cta: 'Start free trial',
  },
  {
    name: 'Pro',
    price: '$40',
    desc: 'For growing teams that need more power.',
    features: ['Everything in Starter', 'LinkedIn outreach', 'Advanced analytics', 'Team collaboration', 'Priority support'],
    cta: 'Start free trial',
    popular: true,
  },
  {
    name: 'Agency',
    price: '$100',
    desc: 'For agencies managing multiple clients.',
    features: ['Everything in Pro', 'Multi-workspace', 'Client management', 'White-label reports', 'Dedicated support'],
    cta: 'Start free trial',
  },
];

const STEPS = [
  { step: '1', title: 'Upload your leads', desc: 'Import a CSV or add leads manually. OutboundOS takes it from there.' },
  { step: '2', title: 'AI researches & writes', desc: 'Claude researches each lead and composes personalized outreach tailored to them.' },
  { step: '3', title: 'Launch & track', desc: 'Schedule your sequence, sit back, and watch replies roll in with full analytics.' },
];

export default function Landing() {
  const navigate = useNavigate();
  const goSignup = () => navigate('/login?mode=signup');

  return (
    <div className="min-h-screen bg-white">
      {/* Nav */}
      <nav className="sticky top-0 z-50 bg-white/80 backdrop-blur border-b border-slate-100">
        <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
          <div>
            <span className="font-bold text-xl tracking-tight text-slate-900">OutboundOS</span>
            <span className="text-slate-400 text-xs ml-2 hidden sm:inline">A CSuite Code Tool</span>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={() => navigate('/docs')} className="text-sm text-slate-600 hover:text-slate-900 font-medium px-3 py-2">
              Docs
            </button>
            <button onClick={() => navigate('/login')} className="text-sm text-slate-600 hover:text-slate-900 font-medium px-3 py-2">
              Sign in
            </button>
            <button onClick={goSignup} className="text-sm font-medium px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-700 text-white transition-colors">
              Get started free
            </button>
          </div>
        </div>
      </nav>

      {/* Hero */}
      <section className="pt-20 pb-24 px-6">
        <div className="max-w-3xl mx-auto text-center">
          <div className="inline-flex items-center gap-2 bg-brand-50 text-brand-700 text-xs font-semibold px-3 py-1 rounded-full mb-6">
            <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" /></svg>
            Powered by Claude AI
          </div>
          <h1 className="text-4xl sm:text-5xl lg:text-6xl font-extrabold text-slate-900 tracking-tight leading-[1.1]">
            Cold outreach that<br />
            <span className="text-brand-600">actually gets replies</span>
          </h1>
          <p className="mt-6 text-lg sm:text-xl text-slate-500 max-w-2xl mx-auto leading-relaxed">
            OutboundOS uses AI to research your leads, write personalized emails, and run multi-step sequences across email and LinkedIn — on autopilot.
          </p>
          <div className="mt-10 flex flex-col sm:flex-row items-center justify-center gap-4">
            <button onClick={goSignup} className="w-full sm:w-auto text-base font-semibold px-8 py-3.5 rounded-xl bg-brand-600 hover:bg-brand-700 text-white shadow-lg shadow-brand-600/25 transition-all hover:shadow-xl hover:shadow-brand-600/30">
              Start your free trial
            </button>
            <span className="text-sm text-slate-400">14-day free trial. No credit card required.</span>
          </div>
        </div>
      </section>

      {/* Social proof bar */}
      <section className="border-y border-slate-100 bg-slate-50 py-8 px-6">
        <div className="max-w-4xl mx-auto flex flex-wrap items-center justify-center gap-x-12 gap-y-4 text-center">
          <div>
            <p className="text-2xl font-bold text-slate-900">10x</p>
            <p className="text-xs text-slate-500 mt-0.5">faster than manual outreach</p>
          </div>
          <div className="hidden sm:block w-px h-10 bg-slate-200" />
          <div>
            <p className="text-2xl font-bold text-slate-900">3-5x</p>
            <p className="text-xs text-slate-500 mt-0.5">higher reply rates</p>
          </div>
          <div className="hidden sm:block w-px h-10 bg-slate-200" />
          <div>
            <p className="text-2xl font-bold text-slate-900">Zero</p>
            <p className="text-xs text-slate-500 mt-0.5">templates needed</p>
          </div>
          <div className="hidden sm:block w-px h-10 bg-slate-200" />
          <div>
            <p className="text-2xl font-bold text-slate-900">Multi-channel</p>
            <p className="text-xs text-slate-500 mt-0.5">email + LinkedIn in one flow</p>
          </div>
        </div>
      </section>

      {/* How it works */}
      <section className="py-20 px-6">
        <div className="max-w-5xl mx-auto">
          <h2 className="text-3xl font-bold text-slate-900 text-center mb-4">How it works</h2>
          <p className="text-slate-500 text-center mb-14 max-w-xl mx-auto">Three steps to outbound that converts. No copywriting skills required.</p>
          <div className="grid md:grid-cols-3 gap-8">
            {STEPS.map((s) => (
              <div key={s.step} className="relative">
                <div className="w-10 h-10 rounded-full bg-brand-600 text-white flex items-center justify-center text-lg font-bold mb-4">
                  {s.step}
                </div>
                <h3 className="text-lg font-semibold text-slate-900 mb-2">{s.title}</h3>
                <p className="text-slate-500 text-sm leading-relaxed">{s.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="py-20 px-6 bg-slate-50">
        <div className="max-w-5xl mx-auto">
          <h2 className="text-3xl font-bold text-slate-900 text-center mb-4">Everything you need to close deals</h2>
          <p className="text-slate-500 text-center mb-14 max-w-xl mx-auto">An all-in-one outbound platform — from first touch to closed deal.</p>
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-6">
            {FEATURES.map((f) => (
              <div key={f.title} className="bg-white rounded-xl border border-slate-200 p-6 hover:shadow-md transition-shadow">
                <div className="w-10 h-10 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center mb-4">
                  {f.icon}
                </div>
                <h3 className="font-semibold text-slate-900 mb-2">{f.title}</h3>
                <p className="text-sm text-slate-500 leading-relaxed">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" className="py-20 px-6">
        <div className="max-w-5xl mx-auto">
          <h2 className="text-3xl font-bold text-slate-900 text-center mb-4">Simple, transparent pricing</h2>
          <p className="text-slate-500 text-center mb-14 max-w-xl mx-auto">Start free for 14 days. Upgrade when you're ready.</p>
          <div className="grid md:grid-cols-3 gap-6 max-w-4xl mx-auto">
            {PLANS.map((plan) => (
              <div
                key={plan.name}
                className={`rounded-xl border p-6 flex flex-col ${
                  plan.popular
                    ? 'border-brand-600 shadow-lg shadow-brand-600/10 ring-1 ring-brand-600 relative'
                    : 'border-slate-200'
                }`}
              >
                {plan.popular && (
                  <span className="absolute -top-3 left-1/2 -translate-x-1/2 bg-brand-600 text-white text-xs font-semibold px-3 py-1 rounded-full">
                    Most popular
                  </span>
                )}
                <h3 className="text-lg font-semibold text-slate-900">{plan.name}</h3>
                <div className="mt-3 mb-4">
                  <span className="text-4xl font-extrabold text-slate-900">{plan.price}</span>
                  <span className="text-slate-500 text-sm">/mo</span>
                </div>
                <p className="text-sm text-slate-500 mb-6">{plan.desc}</p>
                <ul className="space-y-2.5 mb-8 flex-1">
                  {plan.features.map((f) => (
                    <li key={f} className="flex items-start gap-2 text-sm text-slate-600">
                      <svg className="w-4 h-4 text-emerald-500 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                      </svg>
                      {f}
                    </li>
                  ))}
                </ul>
                <button
                  onClick={goSignup}
                  className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors ${
                    plan.popular
                      ? 'bg-brand-600 hover:bg-brand-700 text-white'
                      : 'bg-slate-100 hover:bg-slate-200 text-slate-900'
                  }`}
                >
                  {plan.cta}
                </button>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="py-20 px-6 bg-slate-900">
        <div className="max-w-2xl mx-auto text-center">
          <h2 className="text-3xl font-bold text-white mb-4">Ready to transform your outbound?</h2>
          <p className="text-slate-400 mb-8 text-lg">Join teams that are closing more deals with AI-powered outreach.</p>
          <button onClick={goSignup} className="text-base font-semibold px-8 py-3.5 rounded-xl bg-white hover:bg-slate-100 text-slate-900 transition-colors">
            Start your free trial
          </button>
          <p className="text-slate-500 text-sm mt-4">14-day free trial. No credit card required.</p>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-slate-100 py-8 px-6">
        <div className="max-w-6xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4">
          <div>
            <span className="font-bold text-slate-900">OutboundOS</span>
            <span className="text-slate-400 text-xs ml-2">A CSuite Code Tool</span>
          </div>
          <p className="text-sm text-slate-400">&copy; {new Date().getFullYear()} CSuite Code. All rights reserved.</p>
        </div>
      </footer>
    </div>
  );
}
