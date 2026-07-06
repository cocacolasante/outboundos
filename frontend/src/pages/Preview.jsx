import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  approveAll as approveAllCampaign,
  getCampaign,
  getPreview,
  rejectPreview,
  updateSample,
} from '../api/campaigns.js';
import EmailPreviewCard from '../components/EmailPreviewCard.jsx';
import { Skeleton, ErrorState } from '../components/states.jsx';

export default function Preview() {
  const { id } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const previewQuery = useQuery({
    queryKey: ['preview', id],
    queryFn: () => getPreview(id),
  });
  const campaignQuery = useQuery({
    queryKey: ['campaign', id],
    queryFn: () => getCampaign(id),
  });

  const approveAllMutation = useMutation({
    mutationFn: () => approveAllCampaign(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['campaign', id] });
      navigate(`/campaigns/${id}`);
    },
  });

  const rejectMutation = useMutation({
    mutationFn: () => rejectPreview(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['campaign', id] });
      navigate(`/campaigns/${id}`);
    },
  });

  async function handleSampleSave(leadId, payload) {
    const updated = await updateSample(id, leadId, payload);
    // Patch the cached preview without a full refetch.
    queryClient.setQueryData(['preview', id], (prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        samples: prev.samples.map((s) =>
          s.lead_id === leadId ? { ...s, ...updated } : s,
        ),
      };
    });
  }

  async function handleApprove(leadId, approved) {
    await handleSampleSave(leadId, { approved });
  }

  if (previewQuery.isLoading || campaignQuery.isLoading) {
    return (
      <div className="p-8 max-w-3xl mx-auto" data-testid="preview-loading">
        <Skeleton className="h-7 w-40 mb-2" />
        <Skeleton className="h-4 w-72 mb-6" />
        <div className="space-y-4">
          {[0, 1, 2].map((i) => (
            <div key={i} className="bg-white rounded-card border border-slate-200 p-5">
              <Skeleton className="h-4 w-1/3 mb-3" />
              <Skeleton className="h-3 w-full mb-2" />
              <Skeleton className="h-3 w-5/6" />
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (previewQuery.error || campaignQuery.error) {
    return (
      <div className="p-8 max-w-3xl mx-auto">
        <ErrorState
          message="Couldn't load the preview."
          onRetry={() => { previewQuery.refetch(); campaignQuery.refetch(); }}
          testId="preview-error"
        />
      </div>
    );
  }

  const preview = previewQuery.data;
  const campaign = campaignQuery.data;
  const samples = preview?.samples || [];
  const approvedCount = samples.filter((s) => s.sample_approved === true).length;
  const total = samples.length;

  return (
    <div className="p-8 max-w-[880px] mx-auto pb-32">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold text-slate-900 m-0">Preview</h1>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => rejectMutation.mutate()}
            disabled={rejectMutation.isPending}
            className="inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors disabled:opacity-50"
          >
            Reject all
          </button>
          <button
            type="button"
            onClick={() => approveAllMutation.mutate()}
            disabled={approveAllMutation.isPending || total === 0}
            className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {approveAllMutation.isPending ? 'Launching…' : 'Approve all & launch'}
          </button>
        </div>
      </div>

      <div data-testid="campaign-context" className="flex gap-8 p-4 bg-slate-50 border border-slate-200 rounded-lg mb-6 text-sm">
        <div><span className="text-slate-500">Goal:</span> <strong className="font-medium text-slate-900">{campaign?.goal}</strong></div>
        <div><span className="text-slate-500">Tone:</span> <strong className="font-medium text-slate-900">{campaign?.tone}</strong></div>
      </div>

      {samples.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p>No samples available yet. Wait for research and composition to complete.</p>
        </div>
      ) : (
        samples.map((sample) => (
          <EmailPreviewCard
            key={sample.lead_id}
            sample={sample}
            remainingSamples={total - 1}
            onSave={handleSampleSave}
            onApprove={handleApprove}
          />
        ))
      )}

      {/* Sticky bottom action bar */}
      <div className="fixed bottom-0 left-0 right-0 bg-white border-t border-slate-200 px-6 py-3 flex items-center justify-between shadow-[0_-2px_8px_rgba(0,0,0,0.05)] z-40">
        <span data-testid="approval-counter" className="text-sm text-slate-600">
          <strong className="font-semibold text-slate-900">{approvedCount}</strong> of {total} approved
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => rejectMutation.mutate()}
            disabled={rejectMutation.isPending}
            className="inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors"
          >
            Reject and reconfigure
          </button>
          <button
            type="button"
            onClick={() => approveAllMutation.mutate()}
            disabled={approveAllMutation.isPending || total === 0}
            className="inline-flex items-center px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {approveAllMutation.isPending ? 'Launching…' : 'Approve and launch campaign'}
          </button>
        </div>
      </div>
    </div>
  );
}
