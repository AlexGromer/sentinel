// Copilot Runtime endpoint — bridges the CopilotKit frontend to Sentinel's OpenAI-compat shim
// (POST /v1/chat/completions, ADR-041). The shim drives Sentinel "as a model": one chat turn = one run.
// The bearer token stays server-side here (never shipped to the browser).
//
// ⚠ VERIFY BEFORE INSTALL: CopilotKit's runtime is at v2 (runtime/react-core 1.61.x) and the exact
// exports below — BuiltInAgent, copilotRuntimeNextJSAppRouterEndpoint, convertMessagesToVercelAISDKMessages —
// are evolving. Confirm them against the current docs (https://docs.copilotkit.ai/backend/copilot-runtime,
// /backend/custom-agent) and the Vercel AI SDK (`ai`, `@ai-sdk/openai`) before `npm install`. This is a
// DEV-ONLY scaffold; it is not built or tested in CI.
import {
  CopilotRuntime,
  BuiltInAgent,
  copilotRuntimeNextJSAppRouterEndpoint,
  convertMessagesToVercelAISDKMessages,
} from "@copilotkit/runtime";
import { streamText } from "ai";
import { createOpenAI } from "@ai-sdk/openai";

// Point an OpenAI-compatible client at the Sentinel control-API shim (its /v1 base).
const sentinel = createOpenAI({
  baseURL: (process.env.CONTROL_API_URL ?? "http://127.0.0.1:8090") + "/v1",
  apiKey: process.env.CONTROL_API_TOKEN ?? "",
});

// One Sentinel run per turn: the model id selects the mode (sentinel | sentinel-goal | sentinel-explore).
const agent = new BuiltInAgent({
  type: "aisdk",
  factory: ({ input, abortSignal }: { input: { messages: unknown[] }; abortSignal: AbortSignal }) =>
    streamText({
      model: sentinel(process.env.SENTINEL_MODEL ?? "sentinel"),
      messages: convertMessagesToVercelAISDKMessages(input.messages),
      abortSignal,
    }),
});

const runtime = new CopilotRuntime({ agents: { sentinel: agent } });

export const POST = async (req: Request) => {
  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime,
    endpoint: "/api/copilotkit",
  });
  return handleRequest(req);
};
