/** Keeps one panel's render error from blanking the whole workspace; shows the error and a retry. */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface State { error: Error | null }

export class ErrorBoundary extends Component<{ label: string; children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[${this.props.label}]`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="m-3 rounded-md border border-bad/40 bg-bad/5 p-3 text-xs">
        <div className="font-semibold text-bad">{this.props.label} failed to render</div>
        <div className="mt-1 font-mono text-muted">{this.state.error.message}</div>
        <button type="button" className="mt-2 text-accent hover:underline" onClick={() => this.setState({ error: null })}>Try again</button>
      </div>
    );
  }
}
