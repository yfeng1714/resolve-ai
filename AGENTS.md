# ResolveAI engineering instructions

Read README.md, IMPLEMENTATION_PLAN.md, docs/PRODUCT_SCOPE.md, docs/ARCHITECTURE.md
and docs/adr/ before starting. User instructions determine authorized scope; planning
documents are not blanket authorization to execute every milestone.

- Work in this repository. Do not publish parent workspace files, personal career
  notes, secrets, local .env, raw traces or unreviewed execution results.
- The project owner handles commits and pushes. Review and implementation are local;
  do not commit, push, publish, or change remote visibility without explicit later
  authorization. Report base HEAD, working-tree diff and untracked files for reviews.
- M0 contains documentation, dependency locks and development database configuration.
  M1 implements the fake-provider refund slice. Do not start M2–M5, paid model calls,
  real integrations, or deployment unless the user explicitly delegates that work.
- Implement small vertical slices. Preserve existing changes; avoid broad rewrites.
- Use Python 3.12 + uv for backend/agent/simulator, React/TypeScript + Vite + pnpm
  for the M1 frontend. The single Python dependency lock applies to the Python components.
- Never trust model-supplied identity, tenant, permission, approval or risk labels.
  Writes consume server-validated proposals; authenticated human sessions approve them.
- Stable action IDs, simulator/business deduplication, approval checks and result
  verification precede the first write path. Unknown writes are queried, never blindly repeated.
- Fake model is the default for development/CI. A zero budget prohibits live provider use.
  A real provider, credentials and explicit total spending limit are required before paid calls.
- Core ends at M2. Subscription/incidents, a second tenant, durable worker/checkpoint,
  hybrid retrieval and full comparative evaluation are Portfolio requirements.
- Record observed plan/tool changes for at least two adaptive task families. Do not
  show private chain of thought. Keep B1 competent and report inconclusive comparisons.
- UI acceptance requires browser interaction; financial outcomes require independent
  database/simulator checks. Builds and tool HTTP 200 responses alone are insufficient.
- Report commands, commit, fixture/model/config versions, results and limitations.
  Do not describe target metrics as measured results or local work as deployed.
- CI, migrations and public deployment must match actual implementation. Do not
  initialize demo schemas or fixtures in an unrelated existing database.
