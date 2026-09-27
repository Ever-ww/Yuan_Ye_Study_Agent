export type ReasoningEffort = "none" | "low" | "medium" | "high" | "xhigh" | "max";

export type Project = {
  project_id: string;
  workspace_id: string;
  name: string;
  path: string;
  version: number;
  migration_version: number;
  created_at: string;
  last_opened_at: string;
};

export type Session = {
  session_id: string;
  created_at: string;
  updated_at?: string;
  first_question?: string | null;
  first_question_at?: string | null;
  latest_file: string;
  message_count: number;
  display_name?: string | null;
};

export type SessionRecord = {
  role: "user" | "assistant" | "tool" | "summary";
  content: string | null;
  timestamp?: string;
  tool_calls?: Array<Record<string, unknown>>;
  name?: string;
  status?: string;
  arguments?: Record<string, unknown>;
  run_id?: string;
  turn_id?: string;
  record_id?: string;
  tool_call_id?: string;
  origin?: string;
  reasoning?: string | null;
  model?: Record<string, unknown>;
  model_call?: ModelCallMetric;
  model_calls?: ModelCallMetric[];
  task_latency_ms?: number;
};

export type ModelCallMetric = {
  latency_ms?: number;
  input_tokens?: {
    context_total?: number | null;
    cached?: number | null;
    cache_miss?: number | null;
    cache_hit_ratio?: number | null;
    [key: string]: unknown;
  };
  output_tokens?: number | null;
  output_tokens_source?: string;
  prefix_cache?: {
    total_tokens?: number | null;
    hit_tokens?: number | null;
    miss_tokens?: number | null;
    hit_ratio?: number | null;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export type InboxItem = {
  item_id: string;
  run_id: string;
  project_id: string;
  session_id: string | null;
  title: string;
  summary: string;
  status: string;
  created_at: string;
  read: boolean;
};

export type GatewayEvent = {
  version: 1 | 2 | 3;
  event_id: string;
  sequence: number;
  timestamp: string;
  project_id: string;
  session_id: string | null;
  run_id: string | null;
  type: string;
  payload: Record<string, unknown>;
  stream_id?: string | null;
  stream_sequence?: number | null;
  event_type?: string | null;
};

export type ModelOption = {
  profile_id: string;
  provider: string;
  model: string;
  selected: boolean;
  reasoning_effort: ReasoningEffort;
  supported_reasoning_efforts: ReasoningEffort[];
  default_reasoning_effort: ReasoningEffort;
  effective_reasoning_effort: ReasoningEffort;
};

export type PendingApproval = {
  approval_id: string;
  run_id: string;
  client_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  state: "pending";
  created_at: string;
  expires_at: string;
};

export type ObserverState = {
  user_problem: string;
  completed_tasks: string[];
  in_progress_task: string;
  current_agent_action: string;
  intent_alignment: { status: "aligned" | "uncertain" | "drifted"; reason: string };
};

export type ObserverStatus = {
  run_id?: string;
  status?: "active" | "finalized" | "failed" | string;
  progress_markdown?: string;
  state?: ObserverState;
  correction_proposal?: {
    proposal_id: string;
    proposed_prompt: string;
    revision: number;
    status: string;
  } | null;
};

export type GatewayStatus = {
  gateway: string;
  version: number;
  provider: string;
  model: string;
  stream: boolean;
  sandbox: boolean;
  sandbox_mode: "os" | "os_lazy" | "docker" | "checkpoint_only" | "pending" | "closed";
  sandbox_backend?: string | null;
  sandbox_shell?: string | null;
  bash_available: boolean;
  sandbox_reason: string | null;
  max_concurrent_runs: number;
  cron: {
    healthy: boolean;
    jobs_total: number;
    jobs_scheduled: number;
    jobs_running: number;
    last_error: string | null;
    heartbeat: {
      status: "stopped" | "running" | "unhealthy";
      interval_seconds: number;
      last_tick_at: string | null;
      next_tick_at: string | null;
      last_error: string | null;
    } | null;
  };
};

export type GatewayErrorBody = {
  detail?: string;
  error?: { code?: string; message?: string; recoverable?: boolean; details?: Record<string, unknown> };
  correlation_id?: string;
};

export type RunCreate = {
  projectId: string;
  task: string;
  sessionId?: string;
  modelProfileId: string;
  reasoningEffort: ReasoningEffort;
  uiContext?: {
    source: "agent" | "read" | "write" | "note";
    translation?: boolean;
    resource?:
      | { kind: "paper"; paper_id: string; logical_path: null; content_hash: string }
      | { kind: "workspace_file"; paper_id?: null; logical_path: string; content_hash: string };
    selection?: {
      selected_text: string;
      page: number | null;
      start_line: number | null;
      end_line: number | null;
      nearby_context?: string;
      locator?: Record<string, unknown>;
    };
    attachments?: TemporaryAttachment[];
  };
};

export type ToolResult = { content?: string; [key: string]: unknown };

export type JsonObject = Record<string, unknown>;

export type CronScheduleInput =
  | { kind: "interval"; interval_seconds: number; run_at: null; expression: null; timezone: string }
  | { kind: "once"; interval_seconds: null; run_at: string; expression: null; timezone: string }
  | { kind: "cron"; interval_seconds: null; run_at: null; expression: string; timezone: string };

export type CronRuntimeProfileInput = {
  allowed_tools: string[];
  allowed_skills: string[];
  sandbox_policy: "read_only" | "checkpointed_workspace";
  preapproved_tools: string[];
  max_parallel_tool_calls: number;
  memory_access: "none" | "project";
  allowed_memory_kinds: string[];
  limits: {
    timeout_seconds: number;
    max_turns: number;
    max_model_calls: number;
    max_tool_calls: number;
    token_budget: number;
  };
};

export type CronJobInput = {
  project_id?: string;
  name: string;
  prompt: string;
  schedule: CronScheduleInput;
  misfire_policy: "skip" | "fire_once" | "catch_up";
  runtime_profile: CronRuntimeProfileInput;
};

export type CodeSessionSummary = {
  code_session_id: string;
  project_id: string;
  status: string;
  branch: string;
  base_commit: string;
  verified_turns: number;
  created_at: string;
  updated_at: string;
  origin_session_id?: string | null;
  origin_run_id?: string | null;
  logical_roots: { source: string; skills: string; hooks: string };
};

export type CodeSessionEvent = {
  version: number;
  sequence: number;
  record_type: string;
  timestamp: string;
  [key: string]: unknown;
};

export type PaperListItem = {
  paper_id: string;
  title: string;
  abstract: string;
  publication_year: number | null;
  venue: string;
  citation_key: string | null;
  authors: Array<{ display_name: string }>;
  tags: string[];
  status: "active" | "archived";
  has_pdf: boolean;
  content_hash: string | null;
  summary_status: "missing" | "queued" | "running" | "completed" | "failed";
  updated_at: string;
};

export type TemporaryAttachment = {
  attachment_id: string;
  filename: string;
  content_hash: string;
  created_at: string;
};

export type PaperSummary = {
  paper_id: string;
  status: "missing" | "queued" | "running" | "completed" | "failed";
  content: string;
  run_id: string | null;
  error: string | null;
  updated_at: string | null;
};

export type PaperInlineAnswer = {
  answer_id: string;
  paper_id: string;
  run_id: string;
  page: number;
  selected_text: string;
  selected_text_hash: string;
  nearby_context: string;
  locator: Record<string, unknown>;
  question: string;
  answer: string;
  status: "pending" | "completed" | "failed";
  error: string | null;
  created_at: string;
  answered_at: string | null;
};

export type NoteNode = {
  note_id: string;
  kind: "note" | "folder";
  name: string;
  parent_id: string | null;
  path: string;
  content: string;
  etag: string;
  tags?: string[];
  file_path?: string;
  links?: Array<{ target: string; resolved: boolean; note_id?: string; name?: string }>;
  backlinks?: Array<{ note_id: string; name: string; path: string }>;
  created_at: string;
  updated_at: string;
};

export type NoteTrashItem = {
  trash_id: string;
  original_note_id: string;
  name: string;
  kind: "note" | "folder";
  original_file_path: string;
  deleted_at: string;
};

export type PaperNote = {
  note_id: string;
  paper_id: string;
  page: number | null;
  selected_text: string;
  selected_text_hash: string;
  locator: Record<string, unknown>;
  note_markdown: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type WorkspaceEntry = {
  name: string;
  path: string;
  kind: "file" | "directory" | "blocked_link";
  size: number;
  modified_at: number;
  blocked: boolean;
  etag?: string;
};

export type WorkspaceTree = {
  path: string;
  entries: WorkspaceEntry[];
  next_cursor: string | null;
};

export type WorkspaceDocument = WorkspaceEntry & {
  content: string;
  etag: string;
};

export type LatexDiagnostic = {
  file: string | null;
  line: number | null;
  severity: "error" | "warning" | "info";
  message: string;
};

export type LatexCompilation = {
  compilation_id: string;
  project_id: string;
  run_id: string;
  operation_id: string;
  attempt_id: string;
  main_path: string;
  engine: "tectonic" | "xelatex";
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
  diagnostics: LatexDiagnostic[];
  error: string | null;
  created_at: string;
  updated_at: string;
};
