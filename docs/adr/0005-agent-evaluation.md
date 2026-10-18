# ADR 0005: Agent capability and evaluation

Status: Accepted for M2/M4. Date: 2026-10-12.

Problem: a tool-using scripted workflow can appear agentic without demonstrating
adaptive decision-making or an advantage over simpler engineering.

Alternatives: judge by demo appearance; presume agents win; paired capability checks
plus fair measured comparison.

Choice: Core requires at least two observation-driven task families and 24 development
smoke cases. Develop B1 early using the same tools/fixtures/safety. Portfolio freezes
160 held-out cases from a 240-case dataset and runs baselines/three ablations.
Preregister at least two task families, benefit metrics and worthwhile differences.
Inspect actual business outcomes, report uncertainty and retain all failures.

Cost: substantial dataset/experiment effort and live-model fees. Update 2026-10-17:
a six-scenario pilot provisionally selected `gpt-6-luna` at low reasoning effort.
The fake provider with a zero budget remains the only default for development and CI.
The pilot's application-ledger total was $0.019876 and is not a provider invoice.
Targets are not results.

Reconsider: inconclusive or unfavorable agent comparisons warrant fixed workflows
or a smaller adaptive branch; mark the agent-benefit target unmet without cherry-picking.
