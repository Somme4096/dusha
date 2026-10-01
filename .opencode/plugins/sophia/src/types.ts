// Types for the Sophia Companion State Gateway API and plugin configuration.

export interface SophiaConfig {
  baseUrl: string;
  apiToken: string;
  autoInject: boolean;
  harness: string;
}

export const CONFIG_KEY = "config";

export const DEFAULT_CONFIG: SophiaConfig = {
  baseUrl: "http://127.0.0.1:8765",
  apiToken: "",
  autoInject: true,
  harness: "opencode",
};

export interface HealthResponse {
  status: string;
  database: string;
  upstream_configured: boolean;
  memory_index?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface IngestMessageRequest {
  harness: string;
  conversation_id: string;
  role: string;
  content: unknown;
  route?: string;
  external_id?: string;
  occurred_at?: string | null;
}

export interface IngestMessageResponse {
  id: number;
  duplicate: boolean;
  conversation_id: number;
  sha256: string;
  affect: unknown | null;
}

export interface MemorySearchRequest {
  query: string;
  limit?: number;
  context_messages?: number;
}

export interface MemoryMessage {
  id: number;
  conversation_id: number;
  role: string;
  text: string;
  occurred_at?: string;
}

export interface MemorySearchResult {
  messages: MemoryMessage[];
}

export interface MemorySearchResponse {
  results: MemorySearchResult[];
}

export interface MemoryIndexResponse {
  [key: string]: unknown;
}

export interface ContextBuildRequest {
  query: string;
  harness?: string;
  conversation_id?: string;
  exclude_message_ids?: number[];
  include_recent?: boolean;
}

export interface ContextIdentity {
  configured: boolean;
  text: string;
  revision: string;
}

export interface ContextInjection {
  version: number;
  identity: ContextIdentity;
  instructions: Record<string, unknown>;
  memory: { evergreen: unknown[]; session: unknown[] };
  emotion: {
    values: Record<string, number>;
    description: string;
    preface: string;
    fingerprints: Record<string, unknown>;
  };
}

export interface ContextBuildResponse {
  injection: string;
  affect: AffectState | Record<string, unknown>;
  evergreen_facts: EvergreenFact[];
  records: unknown[];
  search_hits: unknown[];
  context: ContextInjection;
}

export interface AffectState {
  base: Record<string, number>;
  mood: Record<string, number>;
  last_updated_at?: string;
  last_user_message_at?: string;
  last_proactive_sent_at?: string;
  unanswered_proactive?: number;
  [key: string]: unknown;
}

export interface EvergreenFact {
  fact_id: string;
  revision: number;
  state: string;
  effective_state: string;
  priority: number;
  key?: string;
  text?: string;
  review_due?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface RememberFactRequest {
  key: string;
  text: string;
  priority?: number;
  source_message_id?: number | null;
  reason?: string;
  review_after?: string | null;
  expires_at?: string | null;
}

export interface FactResponse {
  fact: EvergreenFact;
}

export interface ListFactsQuery {
  include_inactive?: boolean;
  due_only?: boolean;
  limit?: number;
}

export interface ListFactsResponse {
  facts: EvergreenFact[];
}

export interface ReviseFactRequest {
  expected_revision: number;
  text?: string;
  priority?: number;
  source_message_id?: number | null;
  reason?: string;
  review_after?: string | null;
  expires_at?: string | null;
}

export interface ForgetFactRequest {
  expected_revision: number;
  reason?: string;
  source_message_id?: number | null;
}

export interface FactHistoryResponse {
  [key: string]: unknown;
}
