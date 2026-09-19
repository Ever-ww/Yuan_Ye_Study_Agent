import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

export class AppErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) { return { error }; }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Workbench render failed", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="fatal-state">
        <AlertTriangle aria-hidden="true" />
        <h1>工作台无法继续显示</h1>
        <p>{this.state.error.message || "前端发生了未知错误。"}</p>
        <button type="button" onClick={() => window.location.reload()}>
          <RotateCcw aria-hidden="true" />重新加载
        </button>
      </main>
    );
  }
}
