"use client";

import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";

// Drives Sentinel "as a model" through the OpenAI-compat shim (ADR-041) via the Copilot Runtime at
// /api/copilotkit. One chat turn = one Sentinel run (brain is one-shot). Put a `target: <url>` line +
// a describe/goal/explore instruction in your message — see docs/M12_CONTRACT.md.
export default function Page() {
  return (
    <CopilotKit runtimeUrl="/api/copilotkit" agent="sentinel">
      <main style={{ maxWidth: 820, margin: "0 auto", height: "100vh" }}>
        <CopilotChat
          labels={{
            title: "Sentinel Co-pilot",
            initial:
              "Describe a UI test and include a `target:` URL — I'll run Sentinel and report the verdict.",
          }}
        />
      </main>
    </CopilotKit>
  );
}
