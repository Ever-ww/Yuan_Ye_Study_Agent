import { CheckCircle2, CircleAlert, CircleDashed } from "lucide-react";

export function StatusMark({ state, label }: { state: "ok" | "warning" | "idle"; label: string }) {
  const Icon = state === "ok" ? CheckCircle2 : state === "warning" ? CircleAlert : CircleDashed;
  return <span className={`status-mark status-${state}`}><Icon aria-hidden="true" />{label}</span>;
}
