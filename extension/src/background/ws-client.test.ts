// ADR-143 — КЛИЕНТСКАЯ половина bearer-рукопожатия. Runs under `node --import tsx --test`.
//
// ЗАЧЕМ ЭТОТ ФАЙЛ. Серверная половина покрыта `cmd/control-api/ws_test.go` с самого начала.
// Клиентская — не была покрыта НИЧЕМ: `grep` по дереву давал только определение (`ws-client.ts`) и
// один производственный импорт из `background/index.ts`. Запись реестра
// [EXTENSION-E2E-RUNS-NOWHERE] называла это следствием того, что e2e нигде не исполняется, — и была
// права лишь наполовину. ADR-143 запустил e2e в CI, но e2e открывает СВОЙ `new WebSocket(...)` из
// контекста service worker и `createWsClient` не трогает вовсе. То есть даже исполняемый e2e эту
// дыру не закрывает, и закрыть её должен отдельный гейт — вот он.
//
// ⚠ САМОЕ ЦЕННОЕ УТВЕРЖДЕНИЕ ЗДЕСЬ — ПРО СЕКРЕТ, А НЕ ПРО ПРОТОКОЛ. Конструктор `WebSocket` бросает
// на субпротоколе с недопустимыми символами, и текст исключения ВСТРАИВАЕТ этот субпротокол — то
// есть `bearer.<токен>`. Пробросить его в сообщение об ошибке значило бы напечатать токен в
// интерфейсе и в журнале. Код это знает и пишет об этом в комментарии; до сих пор ничто не мешало
// следующей правке «улучшить диагностику», подставив `String(e)`.
//
// ⚠ ЧЕГО ЭТОТ ФАЙЛ ДОКАЗАТЬ НЕ МОЖЕТ, сказано прямо. Здесь ПОДДЕЛЬНЫЙ `WebSocket`: настоящее
// рукопожатие RFC6455, отказ сервера с 403 и поведение реального Chromium остаются на e2e
// (`test/e2e/recorder.e2e.mjs`, блок #43) и на `cmd/control-api/ws_test.go`. Здесь утверждается
// ровно то, что решает НАШ код: что он предлагает, что требует в ответ и о чём молчит.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createWsClient } from './ws-client.js';
import { WS_SUBPROTOCOL, wsSubprotocols, type Connection } from '../shared/protocol.js';

type Opened = { url: string; protocols: string | string[] | undefined };

/** Подставной WebSocket: записывает, что ему предложили, и позволяет разыграть ответ сервера. */
function fakeSockets(opts: { throwOnConstruct?: boolean; serverProtocol?: string } = {}) {
  const opened: Opened[] = [];
  const instances: any[] = [];
  class FakeWS {
    static OPEN = 1;
    url: string;
    protocol: string;
    readyState = 1;
    onopen: (() => void) | null = null;
    onmessage: ((e: { data: unknown }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    sent: string[] = [];
    closed = false;
    constructor(url: string, protocols?: string | string[]) {
      opened.push({ url, protocols });
      if (opts.throwOnConstruct) {
        // Ровно то, что делает браузер: текст несёт оскорбительный субпротокол, то есть ТОКЕН.
        throw new SyntaxError(
          `Failed to construct 'WebSocket': The subprotocol '${(protocols as string[])[1]}' is invalid.`);
      }
      this.url = url;
      this.protocol = opts.serverProtocol ?? WS_SUBPROTOCOL;
      instances.push(this);
    }
    send(s: string) { this.sent.push(s); }
    close() { this.closed = true; }
  }
  return { FakeWS, opened, instances };
}

/** Подменить глобальный WebSocket на время одной проверки. */
function withFake(FakeWS: unknown, fn: () => void) {
  const g = globalThis as unknown as { WebSocket?: unknown };
  const prev = g.WebSocket;
  g.WebSocket = FakeWS;
  try { fn(); } finally { g.WebSocket = prev; }
}

function collector() {
  const seen: Array<{ state: Connection; detail?: { session?: string; error?: string } }> = [];
  return {
    seen,
    cb: {
      onConnection: (state: Connection, detail?: { session?: string; error?: string }) =>
        seen.push({ state, detail }),
      onServerMessage: () => {},
    },
  };
}

test('the bearer token rides as the SECOND offered subprotocol, built by the shipping helper', () => {
  // KILLS: любая правка, строящая пару руками — она разойдётся с сервером, который валидирует
  // именно `bearer.<token>` и эхом отдаёт только несекретный элемент.
  const { FakeWS, opened } = fakeSockets();
  const { cb } = collector();
  withFake(FakeWS, () => {
    createWsClient(cb).connect('http://127.0.0.1:8099', 'tok-123');
  });
  assert.equal(opened.length, 1);
  assert.deepEqual(opened[0].protocols, wsSubprotocols('tok-123'));
  assert.equal(opened[0].protocols![0], WS_SUBPROTOCOL, 'первым обязан идти несекретный субпротокол');
  assert.match(opened[0].url, /^ws:\/\/127\.0\.0\.1:8099\/v1\/stream$/);
});

test('a plaintext ws:// to a NON-loopback host is refused, and NOT retried', () => {
  // Токен в открытом ws:// уехал бы по проводу. Отказ — конфигурационный, поэтому реконнекта быть
  // не должно: он крутил бы ту же ошибку до исчерпания попыток.
  // KILLS: снятие проверки LOOPBACK; KILLS: превращение отказа в обычную «временную» ошибку.
  const { FakeWS, opened } = fakeSockets();
  const { seen, cb } = collector();
  withFake(FakeWS, () => {
    createWsClient(cb).connect('http://example.test:8099', 'tok-123');
  });
  assert.equal(opened.length, 0, 'сокет вообще не должен был открываться — токен ушёл бы открытым текстом');
  const err = seen.find((s) => s.state === 'error');
  assert.ok(err, 'отказ не объявлен вовсе');
  assert.match(err!.detail!.error!, /plaintext|wss/i);
});

test('https base upgrades to wss, and IS allowed off loopback', () => {
  // Парная половина: защита не имеет права запрещать законный удалённый control-api.
  // KILLS: отказ по имени хоста вместо отказа по схеме.
  const { FakeWS, opened } = fakeSockets();
  const { cb } = collector();
  withFake(FakeWS, () => {
    createWsClient(cb).connect('https://example.test', 'tok-123');
  });
  assert.equal(opened.length, 1, 'wss к удалённому хосту законен и обязан открываться');
  assert.match(opened[0].url, /^wss:\/\/example\.test\/v1\/stream$/);
});

test('a token with invalid subprotocol characters NEVER reaches the error text', () => {
  // ⚠ ГЛАВНОЕ УТВЕРЖДЕНИЕ ФАЙЛА, и оно про секрет. Браузер встраивает оскорбительный субпротокол —
  // то есть `bearer.<токен>` — в текст исключения. Пробросить его наружу значит напечатать токен.
  // KILLS: `catch (e) { cb.onConnection('error', { error: String(e) }) }` — самая естественная
  // «улучшенная диагностика», какую напишет следующий человек.
  const secret = 'tok with spaces';
  const { FakeWS } = fakeSockets({ throwOnConstruct: true });
  const { seen, cb } = collector();
  withFake(FakeWS, () => {
    createWsClient(cb).connect('http://127.0.0.1:8099', secret);
  });
  const err = seen.find((s) => s.state === 'error');
  assert.ok(err, 'конструктор бросил, а ошибка не объявлена');
  const text = JSON.stringify(seen);
  assert.ok(!text.includes(secret),
    `ТОКЕН УТЁК В СООБЩЕНИЕ ОБ ОШИБКЕ: ${err!.detail!.error}`);
  assert.ok(!text.includes('bearer.'),
    'наружу вышла строка субпротокола — она несёт токен целиком');
});

test('a server that echoes the WRONG subprotocol is rejected and the socket closed', () => {
  // Сервер, не говорящий на нашем протоколе, не должен молча считаться подключённым.
  // ⚠ Это НЕ аутентификация сервера (MITM может отдать ту же константу) — так и записано в коде;
  // здесь утверждается только то, что несовпадение ЗАМЕЧЕНО.
  // KILLS: снятие проверки `sock.protocol !== WS_SUBPROTOCOL`.
  const { FakeWS, instances } = fakeSockets({ serverProtocol: 'something.else' });
  const { seen, cb } = collector();
  withFake(FakeWS, () => {
    createWsClient(cb).connect('http://127.0.0.1:8099', 'tok-123');
    instances[0].onopen!();
  });
  const err = seen.find((s) => s.state === 'error');
  assert.ok(err, 'чужой субпротокол принят молча');
  assert.match(err!.detail!.error!, /subprotocol/i);
  assert.equal(instances[0].closed, true, 'сокет с чужим протоколом остался открытым');
});

test('the session greeting is what turns "connecting" into "connected"', () => {
  // Подключением считается не открытый сокет, а полученное приветствие с идентификатором сессии:
  // до него писать события некуда.
  // KILLS: объявление 'connected' в onopen.
  const { FakeWS, instances } = fakeSockets();
  const { seen, cb } = collector();
  withFake(FakeWS, () => {
    createWsClient(cb).connect('http://127.0.0.1:8099', 'tok-123');
    instances[0].onopen!();
    assert.ok(!seen.some((s) => s.state === 'connected'),
      'подключение объявлено до приветствия сервера');
    instances[0].onmessage!({ data: JSON.stringify({ type: 'session', session: 'sess-1' }) });
  });
  const conn = seen.find((s) => s.state === 'connected');
  assert.ok(conn, 'приветствие с сессией не перевело состояние в connected');
  assert.equal(conn!.detail!.session, 'sess-1');
});
