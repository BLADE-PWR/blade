import { Component, type ReactNode, StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "leaflet/dist/leaflet.css";

import { App } from "./App";
import "./index.css";

// Shows a render error instead of a blank page. Reloading starts again at the session picker.
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="pick">
        <h1>Blade</h1>
        <p className="error">The page crashed: {this.state.error.message}</p>
        <button onClick={() => location.reload()}>Back to sessions</button>
      </main>
    );
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
