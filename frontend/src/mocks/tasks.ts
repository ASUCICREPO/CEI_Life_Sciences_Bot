export interface TaskGroup {
  id: string;
  title: string;
  description: string;
  tasks: TaskDefinition[];
  sciencePriority: number; // lower = shown first in science mode
  businessPriority: number;
}

export interface TaskDefinition {
  id: string;
  title: string;
  description: string;
  examples: string[];
  useCaseRefs: number[]; // reference to source use case numbers
  estimatedTime?: string;
  requiresApproval?: boolean;
}

// ADR: only tasks that open a live agent remain here | every group whose tasks resolved to a mock
// (non-live) chat was removed instead of kept as dead UI (lib/resumeRouting.ts liveTaskIds owns the list).
export const mockTaskGroups: TaskGroup[] = [
  {
    id: 'landscape-analysis',
    title: 'Landscape Analysis',
    description: 'Run a connected competitor, market, or legal and regulatory analysis with a scope you confirm.',
    sciencePriority: 1,
    businessPriority: 1,
    tasks: [
      {
        id: 'analysis-competitor',
        title: 'Competitor landscape analysis',
        description: 'Compare competitors on modality, stage, trial activity, approvals, and patent position. You confirm the scope before it runs.',
        examples: [
          'Compare the competitive landscape for KRAS G12C inhibitors.',
          'Who are the main competitors targeting IL-23 in psoriasis?',
        ],
        useCaseRefs: [],
        estimatedTime: '2 to 6 minutes',
        requiresApproval: true,
      },
      {
        id: 'analysis-market',
        title: 'Market landscape analysis',
        description: 'Assess patient population, standard of care, reimbursement, pipeline density, and market drivers. You confirm the scope before it runs.',
        examples: [
          'Size the market landscape for a first-line NSCLC companion diagnostic.',
          'What does the market look like for cell therapies in multiple myeloma?',
        ],
        useCaseRefs: [],
        estimatedTime: '2 to 6 minutes',
        requiresApproval: true,
      },
      {
        id: 'analysis-legal',
        title: 'Legal and regulatory landscape analysis',
        description: 'Map regulatory pathway, approvals and labels, patent landscape, and coverage risk by jurisdiction. You confirm the scope before it runs.',
        examples: [
          'What is the regulatory and patent landscape for CRISPR-based diagnostics in the US and EU?',
          'Assess the legal landscape for our ADC platform.',
        ],
        useCaseRefs: [],
        estimatedTime: '2 to 6 minutes',
        requiresApproval: true,
      },
    ],
  },
  {
    id: 'live-research-drafting',
    title: 'Research and Drafting',
    description: 'Chat live with the Research agent for cited answers, or the Drafting agent for a document.',
    sciencePriority: 2,
    businessPriority: 2,
    tasks: [
      {
        id: 'live-research',
        title: 'Research chat',
        description: 'Ask the Research agent a scientific question and get a cited, evidence-backed answer.',
        examples: [
          'What is known about CRISPR base editing off-target effects?',
          'Summarize recent evidence on CDK4/6 inhibitor resistance.',
        ],
        useCaseRefs: [],
        estimatedTime: '30 seconds to 3 minutes',
      },
      {
        id: 'live-drafting',
        title: 'Draft a document',
        description: 'Draft a grant, VC pitch, journal article, whitepaper, or LaTeX document with the Drafting agent, grounded in cited research. The agent infers the document type and file formats from your request.',
        examples: [
          'Draft a specific aims section for an NIH R01 on our TP53 approach.',
          'Draft a seed-stage pitch narrative for our KRAS G12C program.',
          'Draft an introduction for a paper on IL-23 inhibition in psoriasis.',
          'Draft a whitepaper on companion diagnostics for first-line NSCLC.',
          'Draft a LaTeX methods section for our CRISPR screen.',
        ],
        useCaseRefs: [],
        estimatedTime: '3 to 10 minutes',
      },
    ],
  },
  {
    id: 'run-a-pipeline',
    title: 'Run a Pipeline',
    description: 'Describe a multi-step request and the Pipeline Manager plans and runs it across agents.',
    sciencePriority: 3,
    businessPriority: 3,
    tasks: [
      {
        id: 'run-pipeline',
        title: 'Run a multi-step pipeline',
        description: 'Give one connected request. The Pipeline Manager proposes a plan, runs it (in sequence or concurrently), and reports live step status.',
        examples: [
          'Research TP53 R175H inhibitors, analyze the competitor landscape, then draft a VC pitch.',
          'Assess the market for our diagnostic, then draft an investor update.',
        ],
        useCaseRefs: [],
        estimatedTime: '5 to 20 minutes',
        requiresApproval: true,
      },
    ],
  },
];
