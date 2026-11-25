import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  type ActionRecord,
  type Proposal,
  type Reply,
  type Run,
  type TicketDetail,
  type TicketSummary,
  type Viewer,
  api,
  formatUsd,
} from "./api";
import { LanguageSelect, useI18n } from "./i18n";

export function App() {
  const [viewer, setViewer] = useState<Viewer | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    api
      .current()
      .then(setViewer)
      .catch(() => setViewer(null))
      .finally(() => setReady(true));
  }, []);

  if (!ready) {
    return <Boot />;
  }
  if (!viewer) {
    return <Login onSuccess={setViewer} />;
  }
  return <Desk viewer={viewer} onSignOut={() => setViewer(null)} />;
}

function Boot() {
  const { t } = useI18n();
  return <p className="boot">{t.opening}</p>;
}

function Login({ onSuccess }: { onSuccess: (viewer: Viewer) => void }) {
  const { t, error: describeError } = useI18n();
  const [email, setEmail] = useState("support@demo.resolveai.test");
  const [password, setPassword] = useState("support-demo-pass");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSuccess(await api.login(email, password));
    } catch (caught) {
      const fallback = caught instanceof ApiError ? caught.message : t.signInFailed;
      const code = caught instanceof ApiError ? caught.code : null;
      setError(describeError(code, fallback ?? t.signInFailed));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <p className="eyebrow">{t.eyebrow}</p>
        <h1>ResolveAI</h1>
        <p className="lede">{t.lede}</p>
        <label>
          {t.email}
          <input value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" />
        </label>
        <label>
          {t.password}
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>
        {error ? <p className="banner">{error}</p> : null}
        <button type="submit" disabled={busy}>
          {t.signIn}
        </button>
        <div className="demo-accounts">
          <p>{t.demoAccounts}</p>
          <button
            type="button"
            onClick={() => {
              setEmail("support@demo.resolveai.test");
              setPassword("support-demo-pass");
            }}
          >
            {t.supportOperator}
          </button>
          <button
            type="button"
            onClick={() => {
              setEmail("approver@demo.resolveai.test");
              setPassword("approver-demo-pass");
            }}
          >
            {t.refundApprover}
          </button>
        </div>
      </form>
    </main>
  );
}

function Desk({ viewer, onSignOut }: { viewer: Viewer; onSignOut: () => void }) {
  const { t, status, error: describeError } = useI18n();
  const [tickets, setTickets] = useState<TicketSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TicketDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshLists = useCallback(async (ticketId: string | null) => {
    const listed = await api.tickets();
    setTickets(listed.tickets);
    if (!ticketId) {
      setDetail(null);
      return;
    }
    setDetail(await api.ticket(ticketId));
  }, []);

  useEffect(() => {
    refreshLists(null).catch((caught: unknown) => {
      setError(describeError(caught instanceof ApiError ? caught.code : null, t.loadFailed));
    });
  }, [refreshLists]);

  const latestJob = detail?.recovery_jobs.at(-1) ?? null;
  const linkedProposal =
    detail?.proposals.find((item) => item.id === latestJob?.proposal_id) ?? null;
  const waitingOnWorker =
    latestJob?.state === "queued" ||
    latestJob?.state === "leased" ||
    (latestJob?.state === "settled" && linkedProposal?.status === "approved");
  useEffect(() => {
    if (!selectedId || !waitingOnWorker) {
      return;
    }
    const timer = window.setInterval(() => {
      refreshLists(selectedId).catch(() => undefined);
    }, 400);
    return () => window.clearInterval(timer);
  }, [refreshLists, selectedId, waitingOnWorker]);

  async function choose(ticketId: string) {
    setSelectedId(ticketId);
    setError(null);
    setBusy(true);
    try {
      setDetail(await api.ticket(ticketId));
    } catch (caught) {
      setError(describeError(caught instanceof ApiError ? caught.code : null, t.openFailed));
    } finally {
      setBusy(false);
    }
  }

  async function run(action: () => Promise<TicketDetail>) {
    setBusy(true);
    setError(null);
    try {
      const next = await action();
      setDetail(next);
      setTickets((await api.tickets()).tickets);
    } catch (caught) {
      setError(describeError(caught instanceof ApiError ? caught.code : null, t.actionFailed));
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    try {
      await api.logout();
    } catch {
      // A missing server session should still return to the sign-in screen.
    }
    onSignOut();
  }

  return (
    <div className="desk">
      <header>
        <div className="brand">
          <span className="mark" aria-hidden="true">
            R
          </span>
          <div>
            <p className="eyebrow">{t.eyebrow}</p>
            <strong>ResolveAI</strong>
          </div>
        </div>
        <div className="who">
          <LanguageSelect />
          <span className={`role role-${viewer.role}`}>
            {viewer.role === "approver" ? t.roleApprover : t.roleSupport}
          </span>
          <span>{viewer.display_name}</span>
          <button type="button" onClick={() => void signOut()}>
            {t.signOut}
          </button>
        </div>
      </header>
      {error ? <p className="banner page-banner">{error}</p> : null}
      <div className="workspace">
        <aside className="queue" aria-label={t.ticketsAria}>
          <h2>{t.queue}</h2>
          {tickets.map((ticket) => (
            <button
              key={ticket.id}
              type="button"
              className={ticket.id === selectedId ? "ticket-button selected" : "ticket-button"}
              onClick={() => void choose(ticket.id)}
            >
              <span>{ticket.subject}</span>
              <small>
                {ticket.customer_label} · {status(ticket.status)}
              </small>
            </button>
          ))}
        </aside>
        {detail ? (
          <TicketPane
            detail={detail}
            busy={busy}
            onInvestigate={() => run(() => api.investigate(detail.ticket.id))}
            onApprove={(proposal) =>
              run(() => api.decide(proposal.id, "approve", proposal.revision, proposal.payload_hash))
            }
            onReject={(proposal) =>
              run(() => api.decide(proposal.id, "reject", proposal.revision, proposal.payload_hash))
            }
            onExecute={(proposal) => run(() => api.execute(proposal.id, proposal.revision, proposal.payload_hash))}
            onRevoke={(approvalId, reason) => run(() => api.revoke(approvalId, reason))}
            onSend={(reply) => run(() => api.sendReply(reply.id, reply.revision, reply.body_hash))}
            onRejectReply={(reply) => run(() => api.rejectReply(reply.id, reply.revision, reply.body_hash))}
            onPostMessage={(body) => run(() => api.postMessage(detail.ticket.id, body))}
            onChoose={(subscriptionId) =>
              run(async () => {
                await api.chooseSubscription(detail.ticket.id, subscriptionId);
                return api.investigate(detail.ticket.id);
              })
            }
            onReport={(report) =>
              run(async () => {
                await api.reportIncident(detail.ticket.id, report);
                return api.investigate(detail.ticket.id);
              })
            }
          />
        ) : (
          <section className="empty">
            <h2>{t.chooseTicket}</h2>
            <p>{t.chooseTicketBody}</p>
          </section>
        )}
      </div>
    </div>
  );
}

function subscriptionOptions(run: Run | null): { id: string; plan_code: string }[] {
  const observation = [...(run?.observations ?? [])]
    .reverse()
    .find((item) => item.tool === "list_subscriptions" && item.status === "ok");
  const rows = observation?.data.subscriptions;
  if (!Array.isArray(rows) || rows.length < 2) {
    return [];
  }
  return rows.flatMap((row) => {
    if (!row || typeof row !== "object") {
      return [];
    }
    const record = row as { id?: string; plan_code?: string; cancel_at_period_end?: boolean };
    if (!record.id || record.cancel_at_period_end) {
      return [];
    }
    return [{ id: record.id, plan_code: record.plan_code ?? record.id }];
  });
}

function IncidentReportForm({
  testId,
  submitLabel,
  defaults,
  busy,
  onReport,
}: {
  testId: string;
  submitLabel: string;
  defaults: {
    service_code: string;
    region_code: string;
    reported_started_at: string;
    request_id: string;
  };
  busy: boolean;
  onReport: (report: {
    service_code: string;
    region_code: string;
    reported_started_at: string;
    request_id?: string;
  }) => void;
}) {
  const { t } = useI18n();
  return (
    <form
      className="update"
      data-testid={testId}
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        const requestId = String(form.get("request_id") || "");
        onReport({
          service_code: String(form.get("service_code") || ""),
          region_code: String(form.get("region_code") || ""),
          reported_started_at: String(form.get("reported_started_at") || ""),
          ...(requestId ? { request_id: requestId } : {}),
        });
      }}
    >
      <label>
        {t.incidentService}
        <input name="service_code" defaultValue={defaults.service_code} />
      </label>
      <label>
        {t.incidentRegion}
        <input name="region_code" defaultValue={defaults.region_code} />
      </label>
      <label>
        {t.incidentStart}
        <input name="reported_started_at" defaultValue={defaults.reported_started_at} />
      </label>
      <label>
        {t.incidentRequest}
        <input name="request_id" defaultValue={defaults.request_id} />
      </label>
      <button type="submit" disabled={busy}>
        {submitLabel}
      </button>
    </form>
  );
}

function TicketPane({
  detail,
  busy,
  onInvestigate,
  onApprove,
  onReject,
  onExecute,
  onRevoke,
  onSend,
  onRejectReply,
  onPostMessage,
  onChoose,
  onReport,
}: {
  detail: TicketDetail;
  busy: boolean;
  onInvestigate: () => void;
  onApprove: (proposal: Proposal) => void;
  onReject: (proposal: Proposal) => void;
  onExecute: (proposal: Proposal) => void;
  onRevoke: (approvalId: string, reason: string) => void;
  onSend: (reply: Reply) => void;
  onRejectReply: (reply: Reply) => void;
  onPostMessage: (body: string) => void;
  onChoose: (subscriptionId: string) => void;
  onReport: (report: {
    service_code: string;
    region_code: string;
    reported_started_at: string;
    request_id?: string;
  }) => void;
}) {
  const run = detail.runs.at(-1) ?? null;
  const combinedActive = detail.combined?.active === true;
  const openProposals = detail.proposals.filter((item) => item.status !== "superseded");
  const proposal = openProposals.at(-1) ?? null;
  const shownProposals = combinedActive ? openProposals : proposal ? [proposal] : [];
  const action =
    detail.actions.find((item) => item.action_id === proposal?.action_id) ?? null;
  const earlierCancellations = combinedActive
    ? []
    : detail.actions.filter(
        (item) =>
          item.action_type === "cancel_at_period_end" && item.action_id !== proposal?.action_id,
      );
  const reply = [...detail.replies].reverse().find((item) => item.status !== "superseded") ?? null;
  const approver = detail.viewer.role === "approver";
  const support = detail.viewer.role === "support";
  const canInvestigate = support && (!run || ["failed", "completed", "handed_off", "cancelled"].includes(run.state));
  const { t, status, author, fill } = useI18n();
  const [update, setUpdate] = useState("");
  const [revokeReason, setRevokeReason] = useState("");
  const [correctTicket, setCorrectTicket] = useState<string | null>(null);
  const correcting = correctTicket === detail.ticket.id;

  return (
    <>
      <section className="case" aria-label={t.ticketAria}>
        <p className="eyebrow">{detail.ticket.customer_label}</p>
        <h2>{detail.ticket.subject}</h2>
        <p className="status-line">{fill(t.ticketStatus, { status: status(detail.ticket.status) })}</p>
        <ol className="thread">
          {detail.messages.map((message) => (
            <li key={message.id} className={`message message-${message.author_kind}`}>
              <span>{author(message.author_kind)}</span>
              <p>{message.body}</p>
            </li>
          ))}
        </ol>
        {support ? (
          <form
            className="update"
            onSubmit={(event) => {
              event.preventDefault();
              const body = update.trim();
              if (!body) {
                return;
              }
              setUpdate("");
              onPostMessage(body);
            }}
          >
            <label>
              {t.addUpdate}
              <textarea value={update} onChange={(event) => setUpdate(event.target.value)} />
            </label>
            <button type="submit" disabled={busy || update.trim() === ""}>
              {t.postUpdate}
            </button>
          </form>
        ) : null}
        {support && detail.incident?.active && !detail.incident.report ? (
          <IncidentReportForm
            testId="incident-report"
            submitLabel={t.recordIncident}
            defaults={{
              service_code: "api",
              region_code: "us-east",
              reported_started_at: "2026-10-15T00:00:00.000000Z",
              request_id: "",
            }}
            busy={busy}
            onReport={onReport}
          />
        ) : null}
        {support && detail.incident?.active && detail.incident.report ? (
          correcting ? (
            <IncidentReportForm
              testId="incident-report-correction"
              submitLabel={t.recordCorrectedIncident}
              defaults={{
                service_code: detail.incident.report.service_code,
                region_code: detail.incident.report.region_code,
                reported_started_at: detail.incident.report.reported_started_at ?? "",
                request_id: detail.incident.report.request_id ?? "",
              }}
              busy={busy}
              onReport={onReport}
            />
          ) : (
            <button type="button" disabled={busy} onClick={() => setCorrectTicket(detail.ticket.id)}>
              {t.correctIncident}
            </button>
          )
        ) : null}
        {detail.incident?.handoff ? (
          <p data-testid="incident-state">
            {detail.incident.handoff.kind} · {detail.incident.handoff.reason_code}
          </p>
        ) : null}
        {detail.incident?.history?.length ? (
          <ul data-testid="incident-history">
            {detail.incident.history.map((item) => (
              <li key={item.id}>
                {item.kind} · {item.reason_code}
              </li>
            ))}
          </ul>
        ) : null}
        {support ? (
          <button type="button" disabled={busy || !canInvestigate} onClick={onInvestigate}>
            {t.investigate}
          </button>
        ) : null}
        {support
          ? subscriptionOptions(run).map((item) => (
              <button
                key={item.id}
                type="button"
                data-testid="choose-subscription"
                disabled={busy}
                onClick={() => onChoose(item.id)}
              >
                {item.plan_code}
              </button>
            ))
          : null}
        {run ? <RunView run={run} process={detail.process} /> : null}
      </section>
      <aside className="decision" aria-label={t.decisionAria}>
        <h2>{t.decisionAria}</h2>
        {detail.combined?.active ? (
          <ul data-testid="combined-halves">
            {detail.combined.halves.map((item) => (
              <li key={item.half}>
                {item.half} · {item.outcome_code}
                {item.reason_code ? ` · ${item.reason_code}` : ""}
              </li>
            ))}
          </ul>
        ) : null}
        {shownProposals.length ? (
          shownProposals.map((proposal) => (
          <article
            className="proposal"
            key={proposal.id}
            data-testid={detail.combined?.active ? "combined-proposal" : undefined}
          >
            <p className={proposal.action_type === "cancel_at_period_end" ? "when" : "amount"}>
              {proposal.amount_cents == null
                ? formatInstant(proposal.effective_at ?? proposal.action_id)
                : formatUsd(proposal.amount_cents)}
            </p>
            {proposal.action_type === "cancel_at_period_end" && proposal.effective_at ? (
              <p className="when-exact">{proposal.effective_at}</p>
            ) : null}
            <p>
              {proposal.action_type === "cancel_at_period_end"
                ? fill(t.cancellationSummary, {
                    revision: proposal.revision,
                    status:
                      proposal.status === "open" ? t.statusProposalOpen : status(proposal.status),
                  })
                : fill(t.refundSummary, {
                    currency: proposal.currency ?? "",
                    revision: proposal.revision,
                    status:
                      proposal.status === "open" ? t.statusProposalOpen : status(proposal.status),
                  })}
            </p>
            <dl>
              <div>
                <dt>{t.policy}</dt>
                <dd>{proposal.policy_version}</dd>
              </div>
              <div>
                <dt>{t.action}</dt>
                <dd>{proposal.action_id}</dd>
              </div>
              <div>
                <dt>{t.hash}</dt>
                <dd>{proposal.payload_hash.slice(0, 16)}</dd>
              </div>
            </dl>
            {proposal ? (
              <BillingEvidence
                proposal={proposal}
                run={detail.runs.find((item) => item.id === proposal.run_id) ?? run}
              />
            ) : null}
            {approver && proposal.status === "open" ? (
              <div className="actions">
                <button type="button" disabled={busy} onClick={() => onApprove(proposal)}>
                  {proposal.action_type === "cancel_at_period_end"
                    ? t.approveCancellation
                    : t.approveRefund}
                </button>
                <button type="button" className="danger" disabled={busy} onClick={() => onReject(proposal)}>
                  {proposal.action_type === "cancel_at_period_end"
                    ? t.rejectCancellation
                    : t.rejectRefund}
                </button>
              </div>
            ) : null}
            {proposal.approval ? (
              <div data-testid="grant-status" data-approval-id={proposal.approval.id}>
                <p>
                  {fill(t.grantStatus, { label: proposal.approval.label })}
                  {proposal.approval.expires_at
                    ? ` · ${fill(t.grantDeadline, { time: proposal.approval.expires_at })}`
                    : ""}
                  {proposal.approval.reason
                    ? ` · ${fill(t.grantRevokedReason, { reason: proposal.approval.reason })}`
                    : ""}
                </p>
                {proposal.grant_history.length > 0 ? (
                  <ol data-testid="grant-history">
                    {proposal.grant_history.map((grant) => (
                      <li key={grant.id}>
                        {grant.label}
                        {grant.expires_at ? ` · ${grant.expires_at}` : ""}
                      </li>
                    ))}
                  </ol>
                ) : null}
              </div>
            ) : null}
            {approver &&
            proposal.status === "approved" &&
            proposal.approval?.label === "expired" &&
            !detail.recovery_jobs.some((job) => job.proposal_id === proposal.id && job.admitted) ? (
              <button type="button" data-testid="renew-approval" disabled={busy} onClick={() => onApprove(proposal)}>
                {t.renewApproval}
              </button>
            ) : null}
            {approver && proposal.approval?.label === "valid" ? (
              <div className="actions">
                <label>
                  {t.revokeReason}
                  <input
                    data-testid="revoke-reason"
                    value={revokeReason}
                    onChange={(event) => setRevokeReason(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="danger"
                  disabled={busy || revokeReason.trim() === ""}
                  onClick={() => onRevoke(proposal.approval?.id ?? "", revokeReason.trim())}
                >
                  {proposal.action_type === "cancel_at_period_end"
                    ? t.revokeCancellation
                    : t.revokeRefund}
                </button>
              </div>
            ) : null}
            {approver && proposal.status === "approved" && proposal.approval?.label === "valid" ? (
              <button type="button" disabled={busy} onClick={() => onExecute(proposal)}>
                {proposal.action_type === "cancel_at_period_end" ? t.scheduleCancellation : t.executeRefund}
              </button>
            ) : null}
            {proposal.action_type === "cancel_at_period_end" ? (
              <p data-testid="cancellation-state">
                {detail.actions.find((item) => item.action_id === proposal.action_id)?.status ===
                "confirmed"
                  ? t.cancellationScheduled
                  : t.cancellationProposed}
              </p>
            ) : null}
            {earlierCancellations.map((item) => (
              <p key={item.action_id} data-testid="cancellation-history">
                {t.earlierCancellation} · {status(item.status)}
              </p>
            ))}
            {!approver && proposal.status === "open" ? (
              <p className="hint">
                {proposal.action_type === "cancel_at_period_end"
                  ? t.supportCannotApproveCancellation
                  : t.supportCannotApprove}
              </p>
            ) : null}
            {detailRecovery(detail, proposal.id) ? (
              <p data-testid="recovery-state">
                {fill(t.recoveryState, { state: detailRecovery(detail, proposal.id) ?? "" })}
              </p>
            ) : null}
            {combinedActive
              ? detail.actions
                  .filter((item) => item.action_id === proposal.action_id)
                  .map((item) => <ActionView key={item.action_id} action={item} />)
              : null}
          </article>
          ))
        ) : (
          <p className="hint">{t.noProposal}</p>
        )}
        {!combinedActive && action ? <ActionView action={action} /> : null}
        {reply ? (
          <article className="reply">
            <h3>{t.reviewedReply}</h3>
            <p>{fill(t.replyMeta, { recipient: reply.recipient, status: status(reply.status) })}</p>
            <pre>{reply.body}</pre>
            {reply.receipt ? <p data-testid="send-receipt">{fill(t.receipt, { id: reply.receipt.receipt_id })}</p> : null}
            {approver && reply.status === "draft" ? (
              <div className="actions">
                <button type="button" disabled={busy} onClick={() => onSend(reply)}>
                  {t.approveAndSend}
                </button>
                <button type="button" className="danger" disabled={busy} onClick={() => onRejectReply(reply)}>
                  {t.rejectReply}
                </button>
              </div>
            ) : null}
            <p className="hint">{t.sendDoesNotClose}</p>
          </article>
        ) : null}
      </aside>
    </>
  );
}

function RunView({ run, process }: { run: Run; process: TicketDetail["process"] }) {
  const { t, status, fill } = useI18n();
  const state = status(run.state);
  const provider =
    !run.provider || run.provider === "fake"
      ? t.fakeProvider
      : [run.provider, process.model_name].filter(Boolean).join(" ");
  return (
    <div className="run">
      <h3>
        {fill(t.investigation, { state })}
        {run.error_code ? ` · ${run.error_code}` : ""}
      </h3>
      <p data-testid="run-budget">
        {fill(t.budgetLine, {
          provider,
          prompt: run.prompt_version || "—",
          calls: run.model_calls,
          callLimit: process.run_max_model_calls,
          tools: run.tool_calls,
          toolLimit: process.run_max_tool_calls,
          replans: run.replans,
          replanLimit: process.run_max_replans,
        })}
      </p>
      <ul>
        {run.observations.map((observation, index) => (
          <li key={`${observation.tool}-${index}`}>
            <strong>{observation.tool}</strong> {observation.status === "ok" ? t.toolOk : t.toolError}
            {observation.reason ? ` — ${observation.reason}` : ""}
          </li>
        ))}
      </ul>
      {citationsOf(run).length > 0 ? (
        <div data-testid="policy-citation">
          <h4>{t.citations}</h4>
          <ul>
            {citationsOf(run).map((citation) => (
              <li
                key={citation.evidence_id}
                data-evidence-id={citation.evidence_id}
                data-source-path={citation.source_path}
                data-heading={citation.heading}
                data-provenance={citation.provenance}
              >
                {citation.title} · {citation.version}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function BillingEvidence({ proposal, run }: { proposal: Proposal; run: Run | null }) {
  const { t } = useI18n();
  if (proposal.action_type === "cancel_at_period_end") {
    return (
      <div className="billing" data-testid="billing-evidence">
        <h3>{t.cancellationEvidence}</h3>
        <p>
          {t.effectiveTime} {formatInstant(proposal.effective_at ?? "")}
        </p>
        <p>
          {t.inputRevision} {proposal.preconditions.input_revision ?? "—"}
        </p>
      </div>
    );
  }
  const invoices = observationRows(run, "list_invoices", "invoices");
  const payments = observationRows(run, "list_payments", "payments").filter((row) => {
    return !proposal.preconditions.event_id || row.event_id === proposal.preconditions.event_id;
  });
  const refunds = observationRows(run, "list_refunds", "refunds");
  const invoiceNumber = proposal.evidence.invoice_number || "—";
  return (
    <div className="billing" data-testid="billing-evidence">
      <h3>{t.billingEvidence}</h3>
      <p>
        {t.invoice} {invoiceNumber} · {t.event} {proposal.preconditions.event_id ?? proposal.target_event_id}
      </p>
      <p>
        {proposal.amount_cents == null
          ? (proposal.effective_at ?? "")
          : `${t.expectedRefund} ${formatUsd(proposal.amount_cents)} ${proposal.currency ?? ""}`}
        {" · "}
        {t.inputRevision} {proposal.preconditions.input_revision ?? "—"}
      </p>
      <p>{t.payments}</p>
      <ul>
        {payments.map((payment) => (
          <li key={String(payment.id)}>
            {formatUsd(Number(payment.amount_cents ?? 0))} · {String(payment.status ?? "")} ·{" "}
            {invoiceLabel(invoices, payment.invoice_id) || invoiceNumber}
          </li>
        ))}
      </ul>
      <p>
        {t.existingRefunds}{" "}
        {refunds.length === 0
          ? t.noRefunds
          : refunds
              .map((refund) => `${formatUsd(Number(refund.amount_cents ?? 0))} ${String(refund.status ?? "")}`)
              .join(", ")}
      </p>
    </div>
  );
}

function observationRows(run: Run | null, tool: string, key: string): Record<string, unknown>[] {
  const found = [...(run?.observations ?? [])].reverse().find((item) => item.tool === tool && item.status === "ok");
  const value = found?.data[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null);
}

function invoiceLabel(invoices: Record<string, unknown>[], invoiceId: unknown): string {
  const match = invoices.find((invoice) => invoice.id === invoiceId);
  return match ? String(match.number ?? "") : "";
}

function citationsOf(run: Run): {
  evidence_id: string;
  title: string;
  version: string;
  source_path: string;
  heading: string;
  provenance: string;
}[] {
  const found = [...run.observations].reverse().find((item) => item.tool === "search_knowledge" && item.status === "ok");
  const rows = found?.data.citations;
  if (!Array.isArray(rows)) {
    return [];
  }
  return rows.flatMap((row) => {
    if (typeof row !== "object" || row === null) {
      return [];
    }
    const citation = row as {
      evidence_id?: unknown;
      title?: unknown;
      version?: unknown;
      source_path?: unknown;
      heading?: unknown;
      provenance?: unknown;
    };
    if (typeof citation.evidence_id !== "string") {
      return [];
    }
    return [
      {
        evidence_id: citation.evidence_id,
        title: String(citation.title ?? ""),
        version: String(citation.version ?? ""),
        source_path: String(citation.source_path ?? ""),
        heading: String(citation.heading ?? ""),
        provenance: String(citation.provenance ?? ""),
      },
    ];
  });
}

function detailRecovery(detail: TicketDetail, proposalId: string): string | null {
  return (
    detail.recovery_jobs.filter((job) => job.proposal_id === proposalId).at(-1)?.state ?? null
  );
}

function formatInstant(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  const formatted = new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    hourCycle: "h23",
  }).format(parsed);
  return `${formatted} UTC`;
}

function ActionView({ action }: { action: ActionRecord }) {
  const { t, status } = useI18n();
  const recovered = action.result?.recovered_from_unknown === true;
  return (
    <article className="ledger">
      <h3>
        {action.action_type === "cancel_at_period_end" ? t.cancellationResult : t.refundResult}
      </h3>
      <p data-testid="action-status">
        {status(action.status)}
        {action.operation_id ? ` · ${action.operation_id}` : ""}
      </p>
      {recovered ? <p>{t.recovered}</p> : null}
      {action.error_code ? <p>{action.error_code}</p> : null}
    </article>
  );
}
