export type TaskMode = 'science' | 'business';

/** Mode-facing copy: the same live agent, framed for a scientist or for a business user. */
export interface TaskCopy {
  title: string;
  description: string;
  examples: string[];
}

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

interface TaskSource extends Omit<TaskDefinition, 'title' | 'description' | 'examples'> {
  copy: Record<TaskMode, TaskCopy>;
}

interface TaskGroupSource extends Omit<TaskGroup, 'title' | 'description' | 'tasks'> {
  copy: Record<TaskMode, Omit<TaskCopy, 'examples'>>;
  tasks: TaskSource[];
}

// ADR: only tasks that open a live agent remain here | every group whose tasks resolved to a mock
// (non-live) chat was removed instead of kept as dead UI (lib/resumeRouting.ts liveTaskIds owns the list).
// ADR: copy is per mode, task ids are not | the same runtime serves both modes (its system prompt
// switches on `mode`), so only the wording and examples change, never the routing.
const taskGroupSources: TaskGroupSource[] = [
  {
    id: 'landscape-analysis',
    sciencePriority: 2,
    businessPriority: 1,
    copy: {
      science: {
        title: 'Evidence Landscape Analysis',
        description: 'Compare research programs, evidence quality, and regulatory science across a field, with a scope you confirm.',
      },
      business: {
        title: 'Landscape Analysis',
        description: 'Run a connected competitor, market, or legal and regulatory analysis for strategic decisions, with a scope you confirm.',
      },
    },
    tasks: [
      {
        id: 'analysis-competitor',
        useCaseRefs: [],
        estimatedTime: '2 to 6 minutes',
        requiresApproval: true,
        copy: {
          science: {
            title: 'Compare research programs',
            description: 'Compare programs on mechanism, modality, trial design, clinical evidence, and published data quality. You confirm the scope before it runs.',
            examples: [
              'Compare the mechanisms and clinical evidence of KRAS G12C inhibitors.',
              'How do IL-23 programs in psoriasis differ in trial design and efficacy data?',
            ],
          },
          business: {
            title: 'Competitor landscape analysis',
            description: 'Compare competitors on stage, trial activity, approvals, patent position, and strategic strengths and weaknesses (SWOT). You confirm the scope before it runs.',
            examples: [
              'Who are the main competitors to our KRAS G12C program, and where are they weak?',
              'Build a SWOT for the IL-23 players in psoriasis.',
            ],
          },
        },
      },
      {
        id: 'analysis-market',
        useCaseRefs: [],
        estimatedTime: '2 to 6 minutes',
        requiresApproval: true,
        copy: {
          science: {
            title: 'Disease and treatment landscape',
            description: 'Assess the patient population, epidemiology, standard of care, unmet medical need, and pipeline density for an indication. You confirm the scope before it runs.',
            examples: [
              'What is the standard of care and unmet need in first-line NSCLC?',
              'Map the cell therapy pipeline in multiple myeloma by mechanism.',
            ],
          },
          business: {
            title: 'Market landscape analysis',
            description: 'Assess market size, reimbursement, pricing, pipeline density, and commercial drivers for a product. You confirm the scope before it runs.',
            examples: [
              'Size the market for a first-line NSCLC companion diagnostic.',
              'What is the commercial opportunity for cell therapies in multiple myeloma?',
            ],
          },
        },
      },
      {
        id: 'analysis-legal',
        useCaseRefs: [],
        estimatedTime: '2 to 6 minutes',
        requiresApproval: true,
        copy: {
          science: {
            title: 'Regulatory science landscape',
            description: 'Map evidence requirements, approved labels, clinical endpoints, and prior art by jurisdiction. You confirm the scope before it runs.',
            examples: [
              'What clinical evidence does the FDA require for CRISPR-based diagnostics?',
              'Which endpoints supported recent ADC approvals in the US and EU?',
            ],
          },
          business: {
            title: 'Legal and regulatory landscape analysis',
            description: 'Map the regulatory pathway, patent landscape, freedom-to-operate, and coverage risk by jurisdiction. You confirm the scope before it runs.',
            examples: [
              'What is the regulatory and patent risk for CRISPR-based diagnostics in the US and EU?',
              'Assess the freedom-to-operate and coverage risk for our ADC platform.',
            ],
          },
        },
      },
    ],
  },
  {
    id: 'live-research-drafting',
    sciencePriority: 1,
    businessPriority: 2,
    copy: {
      science: {
        title: 'Research and Drafting',
        description: 'Get cited answers from the Research agent, or draft scientific documents with the Drafting agent.',
      },
      business: {
        title: 'Research and Drafting',
        description: 'Get cited business answers from the Research agent, or draft investor and partner documents with the Drafting agent.',
      },
    },
    tasks: [
      {
        id: 'live-research',
        useCaseRefs: [],
        estimatedTime: '30 seconds to 3 minutes',
        copy: {
          science: {
            title: 'Research a scientific question',
            description: 'Ask about a gene, target, pathway, mechanism, or study and get a cited, evidence-backed answer with study details and limitations.',
            examples: [
              'What is known about CRISPR base editing off-target effects?',
              'Summarize recent evidence on CDK4/6 inhibitor resistance.',
            ],
          },
          business: {
            title: 'Research a business question',
            description: 'Ask about a market, competitor, reimbursement, or regulatory path and get a cited answer with commercial implications and next steps.',
            examples: [
              'What reimbursement codes exist for liquid biopsy CRC screening?',
              'Which companies have licensed CDK4/6 assets in the last 3 years?',
            ],
          },
        },
      },
      {
        id: 'live-drafting',
        useCaseRefs: [],
        estimatedTime: '3 to 10 minutes',
        copy: {
          science: {
            title: 'Draft a scientific document',
            description: 'Draft a grant, journal article, methods section, research whitepaper, or LaTeX document, grounded in cited research. The agent infers the document type and file formats from your request.',
            examples: [
              'Draft a specific aims section for an NIH R01 on our TP53 approach.',
              'Draft an introduction for a paper on IL-23 inhibition in psoriasis.',
              'Draft a LaTeX methods section for our CRISPR screen.',
            ],
          },
          business: {
            title: 'Draft a business document',
            description: 'Draft a VC pitch, investor brief, market whitepaper, or executive summary, grounded in cited research. The agent infers the document type and file formats from your request.',
            examples: [
              'Draft a seed-stage pitch narrative for our KRAS G12C program.',
              'Draft a market whitepaper on companion diagnostics for first-line NSCLC.',
              'Draft an executive summary of our competitive position for the board.',
            ],
          },
        },
      },
    ],
  },
  {
    id: 'run-a-pipeline',
    sciencePriority: 3,
    businessPriority: 3,
    copy: {
      science: {
        title: 'Run a Pipeline',
        description: 'Describe a multi-step research workflow and the Pipeline Manager plans and runs it across agents.',
      },
      business: {
        title: 'Run a Pipeline',
        description: 'Describe a multi-step business workflow and the Pipeline Manager plans and runs it across agents.',
      },
    },
    tasks: [
      {
        id: 'run-pipeline',
        useCaseRefs: [],
        estimatedTime: '5 to 20 minutes',
        requiresApproval: true,
        copy: {
          science: {
            title: 'Run a multi-step research pipeline',
            description: 'Give one connected research request. The Pipeline Manager proposes a plan, keeps the evidence chain between steps, and reports live step status.',
            examples: [
              'Research TP53 R175H inhibitors, compare their mechanisms, then draft a grant aims page.',
              'Review CRISPR off-target evidence, then draft a methods section for our screen.',
            ],
          },
          business: {
            title: 'Run a multi-step business pipeline',
            description: 'Give one connected business request. The Pipeline Manager proposes a plan, runs it, and turns the findings into a stakeholder-ready deliverable.',
            examples: [
              'Analyze the competitor landscape for TP53 R175H, then draft a VC pitch.',
              'Assess the market for our diagnostic, then draft an investor update.',
            ],
          },
        },
      },
    ],
  },
];

/** Task groups with copy for one mode, sorted by that mode's priority. */
export function taskGroupsForMode(mode: TaskMode): TaskGroup[] {
  const priority = mode === 'business' ? 'businessPriority' : 'sciencePriority';
  return [...taskGroupSources]
    .sort((a, b) => a[priority] - b[priority])
    .map(({ copy, tasks, ...group }) => ({
      ...group,
      ...copy[mode],
      tasks: tasks.map(({ copy: taskCopy, ...task }) => ({ ...task, ...taskCopy[mode] })),
    }));
}

/** Science-mode groups, kept for callers that have no mode. */
export const mockTaskGroups: TaskGroup[] = taskGroupsForMode('science');
