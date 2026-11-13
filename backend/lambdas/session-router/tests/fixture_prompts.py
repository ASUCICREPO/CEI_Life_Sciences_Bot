"""F9 task 5: representative free-text prompts with their expected routed agent.

Used by test_handler.py as a mocked-classifier-response fixture (tests the Router's handling of
a label, not the model's real accuracy) and, separately, as the basis for a manual, non-blocking
accuracy spot check against the real fixed classifier model (CLASSIFIER.md step 6; F09 task 5).
Ambiguous prompts are included deliberately -- they are allowed lower real confidence and are not
expected to all classify identically with a mocked response, which is why they are kept out of the
high-confidence FIXTURE_PROMPTS set and listed separately in AMBIGUOUS_PROMPTS.
"""
from __future__ import annotations

FIXTURE_PROMPTS: tuple[tuple[str, str], ...] = (
    ("Summarize the latest clinical trial evidence for GLP-1 receptor agonists in type 2 diabetes.", "research"),
    ("Find me peer-reviewed papers on CRISPR base editing published in the last two years.", "research"),
    ("Write a one-page executive summary for an NIH SBIR Phase I grant application about our oncology biomarker.", "drafting"),
    ("Draft a VC pitch deck outline for our Series A round in gene therapy manufacturing.", "drafting"),
    ("Who are our top three competitors in the continuous glucose monitoring market, and how do we compare?", "analysis"),
    ("Give me a legal landscape analysis of patent risk for mRNA lipid nanoparticle delivery in the EU.", "analysis"),
    ("Research the competitive landscape, then draft a whitepaper summarizing the findings, then have it reviewed.", "pipeline_manager"),
    ("Run the full pipeline: gather market data, analyze it, and produce a final report with citations.", "pipeline_manager"),
)

# Deliberately ambiguous between two agents; real classifier confidence on these is expected to be
# lower and may legitimately fall below the threshold (B80 ChoicePicker), which is correct behavior,
# not a labeling error -- do not tune the fixture set above to hide this.
AMBIGUOUS_PROMPTS: tuple[str, ...] = (
    "Help me with my biotech project.",
    "Tell me about our competitors and write up a report on them.",
)
