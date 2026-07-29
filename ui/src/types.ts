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
  version: 1;
  event_id: string;
  sequence: number;
  timestamp: string;
  project_id: string;
  session_id: string | null;
  run_id: string;
  type: string;
  payload: Record<string, unknown>;
};

export type GatewayStatus = {
  gateway: string;
  version: number;
  provider: string;
  model: string;
  stream: boolean;
  sandbox: boolean;
  max_concurrent_runs: number;
};
