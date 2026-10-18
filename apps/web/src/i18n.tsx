import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { storedLocale, writeLocale, type Locale } from "./locale";

const en = {
  opening: "Opening the desk…",
  eyebrow: "Synthetic billing desk",
  lede: "Investigate a billing ticket, then let a separate approver accept or reject the refund.",
  email: "Email",
  password: "Password",
  signIn: "Sign in",
  signInFailed: "Sign-in failed",
  demoAccounts: "Demo accounts",
  supportOperator: "Support operator",
  refundApprover: "Refund approver",
  signOut: "Sign out",
  language: "Language",
  queue: "Queue",
  chooseTicket: "Choose a ticket",
  chooseTicketBody: "The queue is the synthetic Northwind billing sample. Nothing here is a live payment.",
  ticketsAria: "Tickets",
  ticketAria: "Ticket",
  decisionAria: "Decision",
  ticketStatus: "Ticket {status}",
  investigate: "Investigate",
  investigation: "Investigation · {state}",
  budgetLine: "{provider} · {prompt} · model calls {calls}/{callLimit} · tools {tools}/{toolLimit} · replans {replans}/{replanLimit}",
  fakeProvider: "Fake model",
  refundSummary: "{currency} refund · revision {revision} · {status}",
  policy: "Policy",
  action: "Action",
  hash: "Hash",
  billingEvidence: "Billing evidence",
  invoice: "Invoice",
  event: "Event",
  expectedRefund: "Expected refund",
  inputRevision: "Input revision",
  payments: "Payments",
  existingRefunds: "Existing refunds",
  noRefunds: "None",
  addUpdate: "Add update",
  postUpdate: "Post update",
  approveRefund: "Approve refund",
  rejectRefund: "Reject refund",
  executeRefund: "Execute refund",
  supportCannotApprove: "A refund approver has to accept this revision. Support cannot approve it.",
  noProposal: "No refund proposal yet.",
  citations: "Policy citations",
  reviewedReply: "Reviewed reply",
  replyMeta: "To {recipient} · {status}",
  receipt: "Receipt {id}",
  approveAndSend: "Approve and send",
  rejectReply: "Reject reply",
  sendDoesNotClose: "Sending the reply does not close the ticket.",
  refundResult: "Refund result",
  recovered: "The write response was lost. The business record confirms the refund.",
  loadFailed: "Could not load tickets",
  openFailed: "Could not open the ticket",
  actionFailed: "The action failed",
  toolOk: "ok",
  toolError: "error",
  roleSupport: "support",
  roleApprover: "approver",
  authorCustomer: "customer",
  authorOperator: "operator",
  authorInternal: "internal",
  authorSystem: "system",
  statusOpen: "open",
  statusProposalOpen: "open",
  statusAwaitingApproval: "awaiting approval",
  statusAwaitingReply: "awaiting reply",
  statusHandedOff: "handed off",
  statusReplySent: "reply sent",
  statusAnalyzing: "analyzing",
  statusGathering: "gathering evidence",
  statusProposing: "proposing",
  statusAwaitingCustomer: "awaiting customer",
  statusExecuting: "executing",
  statusVerifying: "verifying",
  statusAwaitingReplyReview: "awaiting reply review",
  statusCompleted: "completed",
  statusFailed: "failed",
  statusCancelled: "cancelled",
  statusReconciling: "reconciling",
  statusApproved: "approved",
  statusRejected: "rejected",
  statusSuperseded: "superseded",
  statusExecuted: "executed",
  statusStale: "stale",
  statusDraft: "draft",
  statusSent: "sent",
  statusPending: "pending",
  statusConfirmed: "confirmed",
  statusUnknown: "unknown",
  errInvalidCredentials: "Sign-in failed",
  errUnauthenticated: "Sign in required",
  errCsrf: "The request was rejected",
  errDenied: "You do not have permission for this action",
  errNotFound: "That record was not found",
  errStaleProposal: "This revision no longer matches",
  errStalePrecondition: "The payment changed after approval",
  errPolicyRejected: "The live refund rule does not match this proposal",
  errNotApproved: "This revision is not approved",
  errRunInProgress: "This ticket already has an open investigation",
  errLiveModelBlocked: "Live model calls are blocked",
  errUnknown: "The refund result is still unknown",
  errUnavailable: "Billing records could not be read",
  errValidation: "The request was rejected",
  errPayloadConflict: "This action was already used with different details",
  errAlreadyExecuted: "This payment already has an executed refund",
  errAlreadySent: "A sent reply cannot be rejected",
};

const zh = {
  opening: "正在打开工作台…",
  eyebrow: "合成账单工作台",
  lede: "先调查账单工单，再由另一位审批人接受或拒绝退款。",
  email: "邮箱",
  password: "密码",
  signIn: "登录",
  signInFailed: "登录失败",
  demoAccounts: "演示账号",
  supportOperator: "客服",
  refundApprover: "退款审批人",
  signOut: "退出",
  language: "语言",
  queue: "队列",
  chooseTicket: "选择一张工单",
  chooseTicketBody: "队列里是 Northwind 的合成账单样本。这里没有任何真实付款。",
  ticketsAria: "工单",
  ticketAria: "工单",
  decisionAria: "决定",
  ticketStatus: "工单 {status}",
  investigate: "调查",
  investigation: "调查 · {state}",
  budgetLine: "{provider} · {prompt} · 模型调用 {calls}/{callLimit} · 工具 {tools}/{toolLimit} · 重新规划 {replans}/{replanLimit}",
  fakeProvider: "模拟模型",
  refundSummary: "{currency} 退款 · 修订 {revision} · {status}",
  policy: "政策",
  action: "操作",
  hash: "摘要",
  billingEvidence: "账单证据",
  invoice: "发票",
  event: "事件",
  expectedRefund: "预计退款",
  inputRevision: "输入版本",
  payments: "付款",
  existingRefunds: "已有退款",
  noRefunds: "无",
  addUpdate: "补充说明",
  postUpdate: "提交更新",
  approveRefund: "批准退款",
  rejectRefund: "拒绝退款",
  executeRefund: "执行退款",
  supportCannotApprove: "这一修订必须由退款审批人接受。客服不能批准。",
  noProposal: "还没有退款提案。",
  citations: "政策引用",
  reviewedReply: "待审核回复",
  replyMeta: "收件人 {recipient} · {status}",
  receipt: "回执 {id}",
  approveAndSend: "批准并发送",
  rejectReply: "拒绝回复",
  sendDoesNotClose: "发送回复不会关闭工单。",
  refundResult: "退款结果",
  recovered: "写入响应已丢失。业务记录确认这笔退款已经完成。",
  loadFailed: "无法加载工单",
  openFailed: "无法打开这张工单",
  actionFailed: "操作失败",
  toolOk: "成功",
  toolError: "失败",
  roleSupport: "客服",
  roleApprover: "审批人",
  authorCustomer: "客户",
  authorOperator: "客服",
  authorInternal: "内部",
  authorSystem: "系统",
  statusOpen: "待处理",
  statusProposalOpen: "待决定",
  statusAwaitingApproval: "等待审批",
  statusAwaitingReply: "等待回复",
  statusHandedOff: "已移交",
  statusReplySent: "回复已发送",
  statusAnalyzing: "分析中",
  statusGathering: "收集证据",
  statusProposing: "拟定提案",
  statusAwaitingCustomer: "等待客户",
  statusExecuting: "执行中",
  statusVerifying: "核对结果",
  statusAwaitingReplyReview: "等待回复审核",
  statusCompleted: "已完成",
  statusFailed: "失败",
  statusCancelled: "已取消",
  statusReconciling: "核对中",
  statusApproved: "已批准",
  statusRejected: "已拒绝",
  statusSuperseded: "已取代",
  statusExecuted: "已执行",
  statusStale: "已过期",
  statusDraft: "草稿",
  statusSent: "已发送",
  statusPending: "处理中",
  statusConfirmed: "已确认",
  statusUnknown: "未知",
  errInvalidCredentials: "登录失败",
  errUnauthenticated: "需要登录",
  errCsrf: "请求被拒绝",
  errDenied: "你没有执行此操作的权限",
  errNotFound: "没有找到这条记录",
  errStaleProposal: "这一修订已经对不上",
  errStalePrecondition: "批准之后付款状态发生了变化",
  errPolicyRejected: "当前退款规则与该提案不一致",
  errNotApproved: "这一修订尚未批准",
  errRunInProgress: "这张工单已有未完成的调查",
  errLiveModelBlocked: "已阻止实时模型调用",
  errUnknown: "退款结果仍然未知",
  errUnavailable: "无法读取账单记录",
  errValidation: "请求被拒绝",
  errPayloadConflict: "这个操作标识已经用于不同的内容",
  errAlreadyExecuted: "这笔付款已有执行过的退款",
  errAlreadySent: "已发送的回复不能拒绝",
} satisfies typeof en;

export type Copy = typeof en;

const copies: Record<Locale, Copy> = { en, "zh-CN": zh };

const statusKeys: Record<string, keyof Copy> = {
  open: "statusOpen",
  awaiting_approval: "statusAwaitingApproval",
  awaiting_reply: "statusAwaitingReply",
  handed_off: "statusHandedOff",
  reply_sent: "statusReplySent",
  analyzing: "statusAnalyzing",
  gathering_evidence: "statusGathering",
  proposing: "statusProposing",
  awaiting_customer: "statusAwaitingCustomer",
  executing: "statusExecuting",
  verifying: "statusVerifying",
  awaiting_reply_review: "statusAwaitingReplyReview",
  completed: "statusCompleted",
  failed: "statusFailed",
  cancelled: "statusCancelled",
  reconciling: "statusReconciling",
  approved: "statusApproved",
  rejected: "statusRejected",
  superseded: "statusSuperseded",
  executed: "statusExecuted",
  stale: "statusStale",
  draft: "statusDraft",
  sent: "statusSent",
  pending: "statusPending",
  confirmed: "statusConfirmed",
  unknown: "statusUnknown",
};

const errorKeys: Record<string, keyof Copy> = {
  invalid_credentials: "errInvalidCredentials",
  unauthenticated: "errUnauthenticated",
  csrf: "errCsrf",
  denied: "errDenied",
  not_found: "errNotFound",
  stale_proposal: "errStaleProposal",
  stale_precondition: "errStalePrecondition",
  policy_rejected: "errPolicyRejected",
  not_approved: "errNotApproved",
  run_in_progress: "errRunInProgress",
  live_model_blocked: "errLiveModelBlocked",
  unknown: "errUnknown",
  unavailable: "errUnavailable",
  validation: "errValidation",
  payload_conflict: "errPayloadConflict",
  already_executed: "errAlreadyExecuted",
  already_sent: "errAlreadySent",
};

const authorKeys: Record<string, keyof Copy> = {
  customer: "authorCustomer",
  operator: "authorOperator",
  internal: "authorInternal",
  system: "authorSystem",
};

type LocaleValue = {
  locale: Locale;
  t: Copy;
  setLocale: (locale: Locale) => void;
  status: (value: string) => string;
  author: (value: string) => string;
  error: (code: string | null, fallback: string) => string;
  fill: (template: string, values: Record<string, string | number>) => string;
};

const LocaleContext = createContext<LocaleValue | null>(null);

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => storedLocale());
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);
  const value = useMemo<LocaleValue>(() => {
    const t = copies[locale];
    return {
      locale,
      t,
      setLocale(next) {
        writeLocale(next);
        setLocaleState(next);
      },
      status(code) {
        const key = statusKeys[code];
        return key ? t[key] : code.replaceAll("_", " ");
      },
      author(code) {
        const key = authorKeys[code];
        return key ? t[key] : code;
      },
      error(code, fallback) {
        const key = code ? errorKeys[code] : undefined;
        return key ? t[key] : fallback;
      },
      fill(template, values) {
        return Object.entries(values).reduce(
          (text, [key, item]) => text.replaceAll(`{${key}}`, String(item)),
          template,
        );
      },
    };
  }, [locale]);
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useI18n(): LocaleValue {
  const value = useContext(LocaleContext);
  if (!value) {
    throw new Error("LocaleProvider is missing");
  }
  return value;
}

export function LanguageSelect() {
  const { locale, setLocale, t } = useI18n();
  return (
    <div className="language" role="radiogroup" aria-label={t.language}>
      <button
        type="button"
        role="radio"
        aria-checked={locale === "en"}
        onClick={() => setLocale("en")}
      >
        English
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={locale === "zh-CN"}
        onClick={() => setLocale("zh-CN")}
      >
        简体中文
      </button>
    </div>
  );
}
