export type Project = {
  project_id: string;
  name: string;
  path: string;
  created_at: string;
  last_opened_at: string;
};

export type Session = {
  session_id: string;
  created_at: string;
  latest_file: string;
  message_count: number;
};

export type SessionRecord = {
  role: "user" | "assistant" | "tool" | "summary";
  content: string | null;
  timestamp?: string;
  tool_calls?: unknown[];
  name?: string;
  run_id?: string;
  record_id?: string;
  origin?: string;
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
  version: 1 | 2;
  event_id: string;
  sequence: number;
  timestamp: string;
  project_id: string;
  session_id: string | null;
  run_id: string;
  type: string;
  payload: Record<string, unknown>;
};

export type ObserverState = {
  user_problem: string;
  completed_tasks: string[];
  in_progress_task: string;
  current_agent_action: string;
  intent_alignment: { status: "aligned" | "uncertain" | "drifted"; reason: string };
};

export type GatewayStatus = {
  gateway: string;
  version: number;
  provider: string;
  model: string;
  stream: boolean;
  sandbox: boolean;
  sandbox_mode: "os" | "docker" | "checkpoint_only" | "pending" | "closed";
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
