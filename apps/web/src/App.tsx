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
            onSend={(reply) => run(() => api.sendReply(reply.id, reply.revision, reply.body_hash))}
            onRejectReply={(reply) => run(() => api.rejectReply(reply.id, reply.revision, reply.body_hash))}
            onPostMessage={(body) => run(() => api.postMessage(detail.ticket.id, body))}
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

function TicketPane({
  detail,
  busy,
  onInvestigate,
  onApprove,
  onReject,
  onExecute,
  onSend,
  onRejectReply,
  onPostMessage,
}: {
  detail: TicketDetail;
  busy: boolean;
  onInvestigate: () => void;
  onApprove: (proposal: Proposal) => void;
  onReject: (proposal: Proposal) => void;
  onExecute: (proposal: Proposal) => void;
  onSend: (reply: Reply) => void;
  onRejectReply: (reply: Reply) => void;
  onPostMessage: (body: string) => void;
}) {
  const run = detail.runs.at(-1) ?? null;
  const proposal = [...detail.proposals].reverse().find((item) => item.status !== "superseded") ?? null;
  const proposalRun = detail.runs.find((item) => item.id === proposal?.run_id) ?? run;
  const action = detail.actions.find((item) => item.action_type === "refund") ?? null;
  const reply = [...detail.replies].reverse().find((item) => item.status !== "superseded") ?? null;
  const approver = detail.viewer.role === "approver";
  const support = detail.viewer.role === "support";
  const canInvestigate = support && (!run || ["failed", "completed", "handed_off", "cancelled"].includes(run.state));
  const { t, status, author, fill } = useI18n();
  const [update, setUpdate] = useState("");

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
        {support ? (
          <button type="button" disabled={busy || !canInvestigate} onClick={onInvestigate}>
            {t.investigate}
          </button>
        ) : null}
        {run ? <RunView run={run} process={detail.process} /> : null}
      </section>
      <aside className="decision" aria-label={t.decisionAria}>
        <h2>{t.decisionAria}</h2>
        {proposal ? (
          <article className="proposal">
            <p className="amount">{formatUsd(proposal.amount_cents)}</p>
            <p>
              {fill(t.refundSummary, {
                currency: proposal.currency,
                revision: proposal.revision,
                status: proposal.status === "open" ? t.statusProposalOpen : status(proposal.status),
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
            {proposal ? <BillingEvidence proposal={proposal} run={proposalRun} /> : null}
            {approver && proposal.status === "open" ? (
              <div className="actions">
                <button type="button" disabled={busy} onClick={() => onApprove(proposal)}>
                  {t.approveRefund}
                </button>
                <button type="button" className="danger" disabled={busy} onClick={() => onReject(proposal)}>
                  {t.rejectRefund}
                </button>
              </div>
            ) : null}
            {approver && proposal.status === "approved" ? (
              <button type="button" disabled={busy} onClick={() => onExecute(proposal)}>
                {t.executeRefund}
              </button>
            ) : null}
            {!approver && proposal.status === "open" ? <p className="hint">{t.supportCannotApprove}</p> : null}
          </article>
        ) : (
          <p className="hint">{t.noProposal}</p>
        )}
        {action ? <ActionView action={action} /> : null}
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
              <li key={citation.evidence_id}>
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
        {t.expectedRefund} {formatUsd(proposal.amount_cents)} {proposal.currency} · {t.inputRevision}{" "}
        {proposal.preconditions.input_revision ?? "—"}
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

function citationsOf(run: Run): { evidence_id: string; title: string; version: string }[] {
  const found = [...run.observations].reverse().find((item) => item.tool === "search_knowledge" && item.status === "ok");
  const rows = found?.data.citations;
  if (!Array.isArray(rows)) {
    return [];
  }
  return rows.flatMap((row) => {
    if (typeof row !== "object" || row === null) {
      return [];
    }
    const citation = row as { evidence_id?: unknown; title?: unknown; version?: unknown };
    if (typeof citation.evidence_id !== "string") {
      return [];
    }
    return [
      {
        evidence_id: citation.evidence_id,
        title: String(citation.title ?? ""),
        version: String(citation.version ?? ""),
      },
    ];
  });
}

function ActionView({ action }: { action: ActionRecord }) {
  const { t, status } = useI18n();
  const recovered = action.result?.recovered_from_unknown === true;
  return (
    <article className="ledger">
      <h3>{t.refundResult}</h3>
      <p data-testid="action-status">
        {status(action.status)}
        {action.operation_id ? ` · ${action.operation_id}` : ""}
      </p>
      {recovered ? <p>{t.recovered}</p> : null}
      {action.error_code ? <p>{action.error_code}</p> : null}
    </article>
  );
}
