import { useLocation } from 'react-router-dom';
import { useApp } from '../context/AppContext';
import { LiveAgentChat } from '../components/LiveAgentChat';
import { ViewMode } from '../design-system/tokens';

// F9: one fixed free-text thread; LiveAgentChat omits `agent` so every turn is classified by the
// Session Router (research/drafting/analysis/pipeline_manager) instead of a deterministic task route.
const ASK_THREAD_KEY = 'ask';

const ASK_EXAMPLES = [
  'What is the mechanism of action of EGFR inhibitors?',
  'Draft a one-page whitepaper on CRISPR delivery methods',
  'Compare the competitive landscape for GLP-1 therapies',
  'Plan a multi-step evidence review for a new biomarker',
];

export function AskPage() {
  const { mode } = useApp();
  const location = useLocation();
  // Home's prompt box navigates here with the typed text; a direct visit has no initial prompt.
  const initialPrompt = (location.state as { prompt?: string } | null)?.prompt;
  const agentMode = mode === ViewMode.BUSINESS ? 'business' : 'science';

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 overflow-hidden">
        <LiveAgentChat
          mode={agentMode}
          threadKey={ASK_THREAD_KEY}
          title="Ask Founder AI"
          description="Describe what you need in plain language. Founder AI picks the best-fit specialist automatically."
          examples={ASK_EXAMPLES}
          placeholder="Ask anything, research, drafting, analysis, or a multi-step plan..."
          autoSendPrompt={initialPrompt}
        />
      </div>
    </div>
  );
}
