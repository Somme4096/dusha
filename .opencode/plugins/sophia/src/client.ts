import type {
  AffectState,
  ContextBuildRequest,
  ContextBuildResponse,
  FactHistoryResponse,
  FactResponse,
  ForgetFactRequest,
  HealthResponse,
  IngestMessageRequest,
  IngestMessageResponse,
  ListFactsQuery,
  ListFactsResponse,
  MemoryIndexResponse,
  MemorySearchRequest,
  MemorySearchResponse,
  RememberFactRequest,
  ReviseFactRequest,
  SophiaConfig,
} from "./types";
import { DEFAULT_CONFIG } from "./types";

function timeoutMsFrom(seconds: unknown): number {
  const valid = typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0;
  return (valid ? seconds : DEFAULT_CONFIG.timeoutSeconds) * 1_000;
}

export interface SophiaClientOptions {
  baseUrl: string;
  apiToken?: string;
  timeoutMs?: number;
}

export class SophiaError extends Error {
  readonly status?: number;
  readonly detail?: unknown;

  constructor(message: string, options?: { status?: number; detail?: unknown; cause?: unknown }) {
    super(message, options?.cause === undefined ? undefined : { cause: options.cause });
    this.name = "SophiaError";
    this.status = options?.status;
    this.detail = options?.detail;
  }
}

type QueryValue = string | number | boolean | undefined;

export function errorMessage(error: unknown): string {
  if (error instanceof SophiaError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

function buildQuery(query?: Record<string, QueryValue>): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) continue;
    params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

function extractDetail(payload: unknown): string {
  if (payload === null || payload === undefined) return "";
  if (typeof payload === "string") return payload;
  if (typeof payload !== "object") return String(payload);
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((entry) => {
        if (entry && typeof entry === "object" && "msg" in entry) return String((entry as { msg: unknown }).msg);
        return JSON.stringify(entry);
      })
      .join(", ");
  }
  try {
    return JSON.stringify(payload);
  } catch {
    return "";
  }
}

export class SophiaClient {
  private readonly baseUrl: string;
  private readonly apiToken: string;
  private readonly timeoutMs: number;

  constructor(options: SophiaClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiToken = options.apiToken ?? "";
    this.timeoutMs = options.timeoutMs ?? timeoutMsFrom(undefined);
  }

  static fromConfig(config: SophiaConfig): SophiaClient {
    return new SophiaClient({
      baseUrl: config.baseUrl,
      apiToken: config.apiToken,
      timeoutMs: timeoutMsFrom(config.timeoutSeconds),
    });
  }

  get url(): string {
    return this.baseUrl;
  }

  private headers(hasBody: boolean): Record<string, string> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (hasBody) headers["Content-Type"] = "application/json";
    if (this.apiToken) headers["X-Companion-Token"] = this.apiToken;
    return headers;
  }

  private describeError(status: number, payload: unknown): string {
    const detail = extractDetail(payload);
    switch (status) {
      case 401:
        return "Authentication failed. Check your API token.";
      case 404:
        return "Resource not found.";
      case 409:
        return "Conflict - resource was modified. Refresh and retry.";
      case 422:
        return detail ? `Invalid request: ${detail}` : "Invalid request.";
      default:
        return detail ? `Sophia request failed (${status}): ${detail}` : `Sophia request failed (${status}).`;
    }
  }

  private async request<T>(
    method: string,
    path: string,
    options?: { body?: unknown; query?: Record<string, QueryValue>; signal?: AbortSignal },
  ): Promise<T> {
    const hasBody = options?.body !== undefined;
    const url = `${this.baseUrl}${path}${buildQuery(options?.query)}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const relayAbort = () => controller.abort();
    options?.signal?.addEventListener("abort", relayAbort, { once: true });

    let response: Response;
    try {
      response = await fetch(url, {
        method,
        headers: this.headers(hasBody),
        body: hasBody ? JSON.stringify(options?.body) : undefined,
        signal: controller.signal,
      });
    } catch (error) {
      if (controller.signal.aborted && !options?.signal?.aborted) {
        throw new SophiaError(`Sophia timed out after ${this.timeoutMs}ms at ${this.baseUrl}.`, { cause: error });
      }
      if (options?.signal?.aborted) {
        throw new SophiaError("Sophia request was cancelled.", { cause: error });
      }
      throw new SophiaError(`Sophia is not reachable at ${this.baseUrl}. Is the gateway running?`, { cause: error });
    } finally {
      clearTimeout(timer);
      options?.signal?.removeEventListener("abort", relayAbort);
    }

    let text = "";
    try {
      text = await response.text();
    } catch {
      text = "";
    }

    let payload: unknown = undefined;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }

    if (!response.ok) {
      throw new SophiaError(this.describeError(response.status, payload), {
        status: response.status,
        detail: payload,
      });
    }

    return payload as T;
  }

  health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("GET", "/health");
  }

  testConnection(): Promise<HealthResponse> {
    return this.health();
  }

  ingestMessage(body: IngestMessageRequest): Promise<IngestMessageResponse> {
    return this.request<IngestMessageResponse>("POST", "/state/v1/messages", { body });
  }

  getMessage(id: number | string): Promise<unknown> {
    return this.request<unknown>("GET", `/state/v1/messages/${encodeURIComponent(String(id))}`);
  }

  searchMemory(body: MemorySearchRequest): Promise<MemorySearchResponse> {
    return this.request<MemorySearchResponse>("POST", "/state/v1/memory/search", { body });
  }

  getMemory(id: number | string, contextMessages?: number): Promise<unknown> {
    return this.request<unknown>("GET", `/state/v1/memory/${encodeURIComponent(String(id))}`, {
      query: { context_messages: contextMessages },
    });
  }

  memoryIndex(): Promise<MemoryIndexResponse> {
    return this.request<MemoryIndexResponse>("GET", "/state/v1/memory/index");
  }

  buildContext(body: ContextBuildRequest): Promise<ContextBuildResponse> {
    return this.request<ContextBuildResponse>("POST", "/state/v1/context", { body });
  }

  getAffect(): Promise<AffectState> {
    return this.request<AffectState>("GET", "/state/v1/affect");
  }

  rememberFact(body: RememberFactRequest): Promise<FactResponse> {
    return this.request<FactResponse>("POST", "/state/v1/evergreen/facts", { body });
  }

  listFacts(query?: ListFactsQuery): Promise<ListFactsResponse> {
    return this.request<ListFactsResponse>("GET", "/state/v1/evergreen/facts", {
      query: query as Record<string, QueryValue>,
    });
  }

  getFactHistory(factId: string): Promise<FactHistoryResponse> {
    return this.request<FactHistoryResponse>(
      "GET",
      `/state/v1/evergreen/facts/${encodeURIComponent(factId)}/history`,
    );
  }

  reviseFact(factId: string, body: ReviseFactRequest): Promise<FactResponse> {
    return this.request<FactResponse>(
      "POST",
      `/state/v1/evergreen/facts/${encodeURIComponent(factId)}/revisions`,
      { body },
    );
  }

  forgetFact(factId: string, body: ForgetFactRequest): Promise<FactResponse> {
    return this.request<FactResponse>(
      "POST",
      `/state/v1/evergreen/facts/${encodeURIComponent(factId)}/forget`,
      { body },
    );
  }
}
