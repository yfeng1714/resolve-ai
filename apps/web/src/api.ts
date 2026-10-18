import { storedLocale } from "./locale";

export type Viewer = {
  email: string;
  display_name: string;
  role: "support" | "approver";
};

export type TicketSummary = {
  id: string;
  subject: string;
  status: string;
  customer_label: string;
  updated_at: string;
};

export type Message = {
  id: string;
  revision: number;
  author_kind: string;
  body: string;
  created_at: string;
};

export type Observation = {
  tool: string;
  status: string;
  reason: string;
  error_code: string | null;
  data: Record<string, unknown>;
  ignored_fields: string[];
};

export type Run = {
  id: string;
  state: string;
  provider: string;
  prompt_version: string;
  model_calls: number;
  tool_calls: number;
  replans: number;
  read_retries: number;
  error_code: string | null;
  observations: Observation[];
  concise_reasons: string[];
};

export type ProcessInfo = {
  model_provider: string;
  model_name: string;
  llm_budget_usd: string;
  allow_live: boolean;
  run_max_model_calls: number;
  run_max_tool_calls: number;
  run_max_replans: number;
};

export type Proposal = {
  id: string;
  run_id: string;
  revision: number;
  status: string;
  action_id: string;
  amount_cents: number;
  currency: string;
  payload_hash: string;
  policy_version: string;
  target_payment_id: string;
  target_event_id: string;
  preconditions: {
    event_id?: string;
    amount_cents?: number;
    currency?: string;
    input_revision?: number;
    payment_resource_version?: number;
  };
  evidence: {
    invoice_number?: string | null;
    customer_name?: string;
  };
};

export type ActionRecord = {
  action_id: string;
  action_type: string;
  status: string;
  operation_id: string | null;
  error_code: string | null;
  result: Record<string, unknown> | null;
};

export type Reply = {
  id: string;
  revision: number;
  status: string;
  recipient: string;
  body: string;
  body_hash: string;
  receipt: { receipt_id: string; sent_at: string } | null;
};

export type TicketDetail = {
  viewer: Viewer;
  ticket: TicketSummary & { customer_email: string };
  messages: Message[];
  runs: Run[];
  proposals: Proposal[];
  actions: ActionRecord[];
  replies: Reply[];
  process: ProcessInfo;
};

type Envelope<T> = {
  status: "ok" | "error";
  data: T | null;
  error_code: string | null;
  message: string | null;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function csrfToken(): string | null {
  const prefix = "resolveai_csrf=";
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-ResolveAI-Locale", storedLocale());
  if (init.body) {
    headers.set("Content-Type", "application/json");
  }
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    const token = csrfToken();
    if (token) {
      headers.set("X-CSRF-Token", token);
    }
  }
  const response = await fetch(`/api${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  const body = (await response.json()) as Envelope<T>;
  if (!response.ok || body.status !== "ok" || body.data === null) {
    throw new ApiError(body.message ?? "Request failed", body.error_code ?? "error", response.status);
  }
  return body.data;
}

export const api = {
  login(email: string, password: string) {
    return request<Viewer>("/sessions", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
  },
  current() {
    return request<Viewer>("/sessions/current");
  },
  logout() {
    return request<{ signed_out: boolean }>("/sessions/current", { method: "DELETE" });
  },
  tickets() {
    return request<{ tickets: TicketSummary[] }>("/tickets");
  },
  ticket(id: string) {
    return request<TicketDetail>(`/tickets/${id}`);
  },
  investigate(id: string) {
    return request<TicketDetail>(`/tickets/${id}/runs`, { method: "POST" });
  },
  decide(id: string, decision: "approve" | "reject", revision: number, payloadHash: string) {
    return request<TicketDetail>(`/proposals/${id}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ revision, payload_hash: payloadHash }),
    });
  },
  execute(id: string, revision: number, payloadHash: string) {
    return request<TicketDetail>(`/proposals/${id}/execute`, {
      method: "POST",
      body: JSON.stringify({ revision, payload_hash: payloadHash }),
    });
  },
  postMessage(id: string, body: string) {
    return request<TicketDetail>(`/tickets/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
  },
  sendReply(id: string, revision: number, bodyHash: string) {
    return request<TicketDetail>(`/reply-drafts/${id}/approve-and-send`, {
      method: "POST",
      body: JSON.stringify({ revision, body_hash: bodyHash }),
    });
  },
  rejectReply(id: string, revision: number, bodyHash: string) {
    return request<TicketDetail>(`/reply-drafts/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ revision, body_hash: bodyHash }),
    });
  },
};

export function formatUsd(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}
