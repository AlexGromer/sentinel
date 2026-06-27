/**
 * Sentinel pw-executor — OpenTelemetry tracing (M8, ADR-021).
 *
 * Gated on OTEL_EXPORTER_OTLP_ENDPOINT (no-op otherwise, zero overhead). Continues the brain's W3C
 * trace by extracting `traceparent` from each tool's `params._meta`, and emits one child span per
 * tool call. stdout stays protocol-only — the OTLP exporter ships spans over gRPC, never stdout.
 */
import { trace, context, propagation, SpanStatusCode, type Tracer, type Context } from '@opentelemetry/api';

let tracer: Tracer | null = null;

export async function setupTracing(): Promise<void> {
  if (!process.env.OTEL_EXPORTER_OTLP_ENDPOINT) return;
  try {
    const { NodeSDK } = await import('@opentelemetry/sdk-node');
    const { OTLPTraceExporter } = await import('@opentelemetry/exporter-trace-otlp-grpc');
    const sdk = new NodeSDK({ traceExporter: new OTLPTraceExporter() });
    sdk.start();
    tracer = trace.getTracer('sentinel.pw-executor');
  } catch (e) {
    console.error('[pw-executor] otel setup failed (tracing off):', e);
  }
}

/** Serialize the active OTel context to a W3C `traceparent` string (M9.5 / M9_CONTRACT §I) for
 * injection into browser requests, so each UI action maps onto the AUT's end-to-end backend trace.
 * Returns undefined when tracing is off (no collector) — callers then skip header injection. */
export function currentTraceparent(): string | undefined {
  if (!tracer) return undefined;
  const carrier: Record<string, string> = {};
  propagation.inject(context.active(), carrier);
  return carrier.traceparent;
}

/** Run `fn` inside a child span parented on the W3C context carried in `meta` (no-op if tracing off). */
export async function spanForTool(
  method: string,
  meta: Record<string, string> | undefined,
  fn: () => Promise<unknown>,
): Promise<unknown> {
  if (!tracer) return fn();
  const parent: Context = meta ? propagation.extract(context.active(), meta) : context.active();
  const span = tracer.startSpan(`tool.${method}`, undefined, parent);
  try {
    return await context.with(trace.setSpan(parent, span), fn);
  } catch (e) {
    span.setStatus({ code: SpanStatusCode.ERROR, message: e instanceof Error ? e.message : String(e) });
    throw e;
  } finally {
    span.end();
  }
}
