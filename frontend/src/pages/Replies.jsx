import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { motion, useReducedMotion } from 'framer-motion';

import { listReplies } from '../api/agent.js';
import { convertLead } from '../api/crm.js';
import { useToast } from '../components/Toast.jsx';
import { Button, PageHeader } from '../components/ui.jsx';
import { EmptyState, ErrorState, Skeleton } from '../components/states.jsx';
import { chipClasses, sentimentSemantic } from '../utils/statusColors.js';
import { riseVariants } from '../utils/motion.js';

function SentimentBadge({ sentiment }) {
  return (
    <span
      data-testid="sentiment-badge"
      className={chipClasses(sentimentSemantic(sentiment))}
    >
      {sentiment || 'unclassified'}
    </span>
  );
}

function ReplyRow({ item, reduceMotion }) {
  const [showDraft, setShowDraft] = useState(false);
  const toast = useToast();
  const queryClient = useQueryClient();

  const convertMutation = useMutation({
    mutationFn: () => convertLead(item.lead_id),
    onSuccess: () => {
      toast.success('Lead converted to opportunity');
      queryClient.invalidateQueries({ queryKey: ['agent-replies'] });
      queryClient.invalidateQueries({ queryKey: ['crm-opportunities'] });
    },
    onError: (err) => {
      toast.error(err?.response?.data?.detail || 'Convert failed');
    },
  });

  const copyDraft = async () => {
    try {
      await navigator.clipboard.writeText(item.draft_body);
      toast.success('Draft copied to clipboard');
    } catch {
      toast.error('Copy failed');
    }
  };

  return (
    <motion.div
      variants={reduceMotion ? undefined : riseVariants}
      data-testid={`reply-row-${item.activity_id}`}
      className="bg-white border border-slate-200 rounded-card shadow-card p-4 flex flex-col gap-2
                 transition-shadow duration-base hover:shadow-card-hover"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <SentimentBadge sentiment={item.sentiment} />
            <span className="font-semibold text-slate-800 truncate">
              {item.lead_name || item.lead_email || 'Unknown sender'}
            </span>
            {item.lead_company && (
              <span className="text-sm text-slate-500">· {item.lead_company}</span>
            )}
            {item.converted && (
              <span data-testid="converted-pill" className={chipClasses('purple')}>
                Converted
              </span>
            )}
          </div>
          <p className="text-sm text-slate-600 mt-1 mb-0 truncate">{item.subject}</p>
          {item.body_preview && (
            <p className="text-sm text-slate-500 mt-1 mb-0 line-clamp-2">{item.body_preview}</p>
          )}
        </div>
        <div className="flex flex-col items-end gap-2 shrink-0">
          <span className="text-xs text-slate-400 tabular">
            {new Date(item.occurred_at).toLocaleString()}
          </span>
          <div className="flex gap-2">
            {item.draft_body && (
              <Button
                variant="secondary"
                size="sm"
                data-testid={`view-draft-btn-${item.activity_id}`}
                aria-expanded={showDraft}
                onClick={() => setShowDraft((s) => !s)}
              >
                {showDraft ? 'Hide draft' : 'View draft'}
              </Button>
            )}
            {item.convert_eligible && (
              <Button
                size="sm"
                data-testid={`convert-btn-${item.activity_id}`}
                onClick={() => convertMutation.mutate()}
                loading={convertMutation.isPending}
              >
                {convertMutation.isPending ? 'Converting…' : 'Convert'}
              </Button>
            )}
          </div>
        </div>
      </div>
      {showDraft && item.draft_body && (
        <div
          data-testid={`draft-panel-${item.activity_id}`}
          className="bg-slate-50 border border-slate-200 rounded-lg p-3"
        >
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
              Suggested reply (AI draft — never sent automatically)
            </span>
            <button
              type="button"
              onClick={copyDraft}
              className="text-xs text-brand-600 hover:text-brand-800 font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded px-1"
            >
              Copy
            </button>
          </div>
          <p className="text-sm text-slate-700 whitespace-pre-wrap mb-0">{item.draft_body}</p>
        </div>
      )}
    </motion.div>
  );
}

export default function Replies() {
  const [sentiment, setSentiment] = useState('');
  const reduceMotion = useReducedMotion();

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['agent-replies', sentiment],
    queryFn: () => listReplies(sentiment ? { sentiment } : {}),
  });

  const items = data?.items || [];

  const filter = (
    <select
      data-testid="sentiment-filter"
      value={sentiment}
      onChange={(e) => setSentiment(e.target.value)}
      className="border border-slate-300 rounded-lg px-3 py-1.5 text-sm bg-white
                 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
    >
      <option value="">All sentiments</option>
      <option value="positive">Positive</option>
      <option value="neutral">Neutral</option>
      <option value="negative">Negative</option>
    </select>
  );

  return (
    <div data-testid="replies-page" className="p-8 max-w-4xl mx-auto">
      <PageHeader
        title="Replies"
        subtitle="Inbound replies the agent classified — triage, convert, and respond. The agent never replies to prospects or converts leads on its own."
        actions={filter}
      />

      {isError ? (
        <ErrorState message="Couldn't load replies." onRetry={refetch} testId="replies-error" />
      ) : isLoading ? (
        <div data-testid="replies-loading" className="flex flex-col gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="bg-white border border-slate-200 rounded-card shadow-card p-4">
              <Skeleton className="h-4 w-1/3 mb-2" />
              <Skeleton className="h-3 w-2/3" />
            </div>
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          testId="replies-empty"
          icon="📭"
          title="No replies yet"
          hint="When a prospect answers one of your campaigns, the agent classifies it and it lands here."
        />
      ) : (
        <motion.div
          className="flex flex-col gap-3"
          initial={reduceMotion ? false : 'hidden'}
          animate="visible"
          variants={reduceMotion ? undefined : { visible: { transition: { staggerChildren: 0.04 } } }}
        >
          {items.map((item) => (
            <ReplyRow key={item.activity_id} item={item} reduceMotion={reduceMotion} />
          ))}
        </motion.div>
      )}

      {(data?.total ?? 0) > items.length && (
        <p className="text-xs text-slate-400 mt-4">
          Showing {items.length} of {data.total}.
        </p>
      )}
      <p className="text-xs text-slate-400 mt-6">
        Looking for what the agent did and why? Every autonomous action is
        audited — see the <Link to="/settings" className="text-brand-600 hover:underline">Agent panel in Settings</Link>.
      </p>
    </div>
  );
}
