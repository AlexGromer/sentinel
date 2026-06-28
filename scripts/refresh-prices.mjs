#!/usr/bin/env node
// Refresh docs/prices.json from OpenRouter's public model catalog.
//
// OpenRouter GET /api/v1/models needs no API key and is CORS-open; `pricing.prompt` and
// `pricing.completion` are USD PER TOKEN (×1e6 → $/1M). We map our model keys to OpenRouter
// slugs below — KEEP IN SYNC with docs/index.html MODELS[].orid / .key. Unmatched models keep
// their existing seed. Prices stay illustrative ("verify") — this only refreshes the numbers.
//
// Run by .github/workflows/prices-refresh.yml (weekly + manual). Node 24+ (global fetch).
import { readFileSync, writeFileSync } from 'node:fs';

// OpenRouter slug -> our prices.json key (mirror of index.html MODELS).
const MAP = {
  'anthropic/claude-opus-4.8':   'opus48',
  'anthropic/claude-sonnet-4.6': 'sonnet46',
  'anthropic/claude-haiku-4.5':  'haiku45',
  'openai/gpt-5.4':              'gpt54',
  'openai/gpt-5.4-mini':         'gpt54mini',
  'openai/o3':                   'o3',
  'x-ai/grok-4.3':               'grok43',
  'z-ai/glm-5':                  'glm5',
  'z-ai/glm-4.7':                'glm47',
  'deepseek/deepseek-v4-flash':  'dsflash',
  'deepseek/deepseek-v4-pro':    'dspro',
  'qwen/qwen-plus':              'qwenplus',
};

const FILE = 'docs/prices.json';

async function main() {
  const cur = JSON.parse(readFileSync(FILE, 'utf8'));
  cur.prices = cur.prices || {};

  const resp = await fetch('https://openrouter.ai/api/v1/models');
  if (!resp.ok) throw new Error('OpenRouter HTTP ' + resp.status);
  const json = await resp.json();
  const byId = {};
  (json.data || []).forEach((m) => { byId[m.id] = m; });

  let n = 0;
  for (const [orid, key] of Object.entries(MAP)) {
    const m = byId[orid];
    if (!m || !m.pricing) continue;
    const i = parseFloat(m.pricing.prompt) * 1e6;
    const o = parseFloat(m.pricing.completion) * 1e6;
    if (Number.isFinite(i) && Number.isFinite(o)) {
      cur.prices[key] = { in: Number(i.toFixed(4)), out: Number(o.toFixed(4)) };
      n++;
    }
  }

  cur._asof = new Date().toISOString().slice(0, 10);
  cur._source = 'OpenRouter /api/v1/models (auto-refresh) + seeds for unmatched models';
  writeFileSync(FILE, JSON.stringify(cur, null, 2) + '\n');
  console.log(`refresh-prices: updated ${n}/${Object.keys(MAP).length} models from OpenRouter (asof ${cur._asof})`);
}

main().catch((e) => { console.error('refresh-prices failed:', e.message); process.exit(1); });
