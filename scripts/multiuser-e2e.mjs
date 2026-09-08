#!/usr/bin/env node
// Сквозной прогон многопользовательского развёртывания: от ПЕРВОГО СТАРТА до прогонов, запущенных
// разными людьми, — со снимком экрана на каждом шаге.
//
// Почему отдельный скрипт, а не ещё одна проверка в ui-smoke. Смоук идёт по развёртыванию, которое
// УЖЕ поднято и в котором аккаунтов нет: он подключается машинным токеном и меряет интерфейс. Всё,
// что эта проверка называет, живёт ДО того момента — как разворачивание заводит первого
// администратора, откуда берётся его пароль, как появляются остальные, и что каждый из них видит.
// Стенду для этого нужны store-gateway и СВОЙ, заведомо пустой, домен аккаунтов; навесить это на
// стенд смоука значит переписать премисы одиннадцати чужих проверок (ровно тот довод, по которому
// проверки ADR-109 в гейте хаба поднимают второй control-API вместо того, чтобы делить первый).
//
// Что здесь утверждается и чего не утверждает больше никто:
//   · развёртывание НЕ начинается с пустой двери — админ заводится сам, пароль печатается один раз;
//   · этим паролем можно войти, и первый аккаунт закрывает анонимное чтение;
//   · каждый пользователь меняет СВОИ настройки прогона и не может тронуть настройки развёртывания;
//   · каждый гонит СВОЙ прогон — на живой модели, когда она задана;
//   · и видит ТОЛЬКО свои прогоны, включая администратора, которому чужой прогон не показан;
//   · и одновременно ПРОТИВОПОЛОЖНОЕ: технический журнал администратор видит целиком.
//
// Две последних строки — пара, и меряются они вместе намеренно. «Админ видит всё» сломает первое,
// «админ видит только своё» — второе, и каждая ошибка по отдельности выглядит как разумная
// реализация. Утверждать надо оба свойства, иначе половина регрессий проходит зелёной.
//
// Прогон:
//   node scripts/multiuser-e2e.mjs --out "$PWD/multiuser-e2e"
//   node scripts/multiuser-e2e.mjs --out … --llm-base http://<host>:11434/v1 --llm-model qwen3:8b
//
// ⚠ Живой модели на раннере GitHub НЕТ и быть не может (эндпоинт живёт в локальной сети
// мейнтейнера). Поэтому половина «прогон спросил модель» не выключается молча: без `--llm-base`
// проверка ОБЪЯВЛЯЕТ пропуск причиной и печатает его — docs/DEVELOPMENT.md §0. Молчаливый пропуск
// и есть тот отказ, ради которого этот файл написан: шаг, рапортующий успех над невыполненной
// работой, хуже отсутствующего шага.

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { hubViews, MIN_VIEWS } from './hub-views.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(REPO, 'pw-executor', 'package.json'));
const { chromium } = require('playwright');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const OUT = arg('out', path.join(REPO, 'multiuser-e2e'));
// СВОЙ порт. Смоук занимает 8090, гейт хаба — 18744/18745; общий прогон CI поднимает их все, и
// стенд, севший на чужой порт, падает с диагнозом про сборку.
const PORT = Number(arg('port', process.env.MU_E2E_PORT || 18760));
const LLM_BASE = arg('llm-base', '');
const LLM_MODEL = arg('llm-model', 'qwen3:8b');
// Фикстура ПО ПУТИ, КОТОРЫЙ ВИДИТ ЭТОТ ПРОЦЕСС (та же поправка, что у ui-smoke: путь внутри
// контейнера верен для компоуза и неверен везде ещё).
const TARGET = arg('target', `file://${path.join(REPO, 'testdata', 'site', 'index.html')}`);

// Токен НЕ короче 16 знаков: с ADR-160 короткий операторский токен — отказ старта (код 2), а не
// предупреждение. Стенд, собранный по старому образцу, не поднялся бы вовсе.
const MACHINE_TOKEN = 'mu-e2e-machine-token-16plus';
const STORE_TOKEN = 'mu-e2e-store-token-16plus';

fs.mkdirSync(OUT, { recursive: true });

const results = [];
const skips = [];
const pageErrors = [];
const consoleErrors = [];
const badResponses = [];
let shotN = 0;

// ПОЛЫ. Обход, переставший что-либо находить, проходит идеально над пустым множеством — это
// единственное, чего сам вывод не ловит (docs/DEVELOPMENT.md §0, принцип 5).
const MIN_ACCOUNTS = 3;          // admin (заводится сам) + двое заведённых
const MIN_OWN_RUNS = 2;          // по прогону на каждого из двоих
// ⚠ РАВЕНСТВО, а не потолок: пропуск здесь ровно один — живая модель, — и он объявлен причиной.
// Порог позволил бы завести второй пропуск молча, то есть ровно то, против чего этот счёт заведён.
const DECLARED_SKIPS_WITHOUT_MODEL = 1;

async function shot(page, name, full = false) {
  const file = path.join(OUT, `${String(++shotN).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: file, fullPage: full });
  return path.basename(file);
}

async function check(name, fn) {
  pageErrors.length = 0;
  try {
    await fn();
    if (pageErrors.length) throw new Error(`uncaught page error(s): ${pageErrors.join(' | ')}`);
    results.push({ name, ok: true });
    console.log(`  ok   ${name}`);
  } catch (e) {
    results.push({ name, ok: false, err: e.message });
    console.log(`  FAIL ${name}\n       ${e.message.split('\n').join('\n       ')}`);
  }
}
function ok(cond, what) { if (!cond) throw new Error(what); }
function eq(got, want, what) { if (got !== want) throw new Error(`${what}: ожидалось ${want}, получено ${got}`); }

function declaredSkip(name, reason) {
  skips.push({ name, reason });
  results.push({ name, ok: true, skipped: true });
  console.log(`  ПРОПУСК ${name}\n       объявленная причина: ${reason}`);
}

// ── стенд ────────────────────────────────────────────────────────────────────────────────────────
let gw = null, capi = null, browser = null;
const storeSock = `/tmp/sentinel-mue2e-${process.pid}.sock`;
const storeDB = path.join(REPO, 'scratch', `mue2e-${process.pid}.db`);
let log = '';

function requireBin(name) {
  const p = path.join(REPO, 'bin', name);
  // НЕ пропуск. Проверка, которая при отсутствии бинаря тихо перестаёт мерить, неотличима от
  // проходящей (ADR-097) — и гейт хаба по этой же причине бросает, а не пропускает.
  if (!fs.existsSync(p)) throw new Error(`${p} не собран — выполните: go build -o bin/${name} ./cmd/${name}`);
  return p;
}

async function bringUp() {
  const capiBin = requireBin('control-api');
  const gwBin = requireBin('store-gateway');
  // ⚠ agentctl гейт хаба НЕ проверяет — он только передаёт путь. Здесь прогон должен дойти до
  // исхода, поэтому отсутствие бинаря обязано называться собой, а не «прогон не завершился».
  const agentctl = requireBin('agentctl');

  // ⚠ БАЗА СНОСИТСЯ ДО СТАРТА, А НЕ ПОСЛЕ. Пароль первого администратора печатается только над
  // ЗАВЕДОМО ПУСТЫМ хранилищем (cmd/control-api/defaultadmin.go: непустой список — молчаливый
  // выход). Уборка «за собой» этого не даёт: второй прогон поверх выжившей базы не увидел бы
  // строки и объявил бы дефектом собственный остаток.
  fs.mkdirSync(path.dirname(storeDB), { recursive: true });
  for (const s of ['', '-shm', '-wal']) { try { fs.unlinkSync(storeDB + s); } catch { /* нет — и хорошо */ } }
  try { fs.unlinkSync(storeSock); } catch { /* нет — и хорошо */ }

  gw = spawn(gwBin, ['--addr', storeSock, '--db', storeDB], {
    cwd: REPO,
    env: { ...process.env, STORE_TOKEN },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  gw.stdout.on('data', (b) => { log += `[gw] ${b}`; });
  gw.stderr.on('data', (b) => { log += `[gw] ${b}`; });
  for (let i = 0; i < 100 && !fs.existsSync(storeSock); i++) await new Promise((r) => setTimeout(r, 50));
  ok(fs.existsSync(storeSock), `сокет store-gateway так и не появился:\n${log.slice(0, 800)}`);

  capi = spawn(capiBin, [], {
    cwd: REPO,
    env: {
      ...process.env,
      CONTROL_API_ADDR: `127.0.0.1:${PORT}`,
      CONTROL_API_SERVE_UI: '1',
      CONTROL_API_UI_DIR: path.join(REPO, 'docs'),
      CONTROL_API_AGENTCTL: agentctl,
      CONTROL_API_CORS_ORIGINS: '',
      CONTROL_API_STORE_ADDR: `unix:${storeSock}`,
      STORE_TOKEN,
      CONTROL_API_TOKEN: MACHINE_TOKEN,
      // ⚠ ОКРУЖЕНИЕ ПРОЦЕССА СТАРШЕ ПЕР-РАННОГО `llm` (cmd/control-api/llmenv.go: precedence
      // process env > per-run > persisted). Если в оболочке уже стоит LLM_BASE_URL — а на этом
      // стенде он стоит, там живая ollama, — то заданное телом прогона молча не сработает, и
      // проверка «прогон спросил ИМЕННО эту модель» окажется зелёной, померив чужую настройку.
      // Пустая строка считается незаданной, поэтому гасим явно и поимённо.
      LLM_BACKEND: '', LLM_MODEL: '', LLM_BASE_URL: '', LLM_API_KEY: '', LLM_VISION: '', LLM_STRUCTURED: '',
      LLM_BACKEND_PLANNER: '', LLM_MODEL_PLANNER: '', LLM_BASE_URL_PLANNER: '',
      LLM_BACKEND_HEAL: '', LLM_MODEL_HEAL: '', LLM_BASE_URL_HEAL: '',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  // ⚠ НАКОПЛЕНИЕ НАЧИНАЕТСЯ ДО ПЕРВОГО fetch. И пароль администратора, и нонс печатаются раньше,
  // чем сервер начинает слушать; подписка после первого удачного /healthz — гонка, в которой
  // теряется ровно то, ради чего этот шаг существует.
  capi.stdout.on('data', (b) => { log += b; });
  capi.stderr.on('data', (b) => { log += b; });

  let up = false;
  for (let i = 0; i < 200; i++) {
    try { if ((await fetch(`http://127.0.0.1:${PORT}/healthz`)).ok) { up = true; break; } } catch { /* ещё не поднялся */ }
    if (/address already in use|bind:/i.test(log)) throw new Error(`порт ${PORT} занят (остался чужой процесс?)`);
    await new Promise((r) => setTimeout(r, 100));
  }
  ok(up, `control-api так и не ответил на /healthz:\n${log.slice(0, 1200)}`);
}

const base = () => `http://127.0.0.1:${PORT}`;
async function api(tokenOrNull, pathname, init) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, (init && init.headers) || {});
  if (tokenOrNull) headers.Authorization = `Bearer ${tokenOrNull}`;
  const r = await fetch(base() + pathname, Object.assign({}, init, { headers }));
  const body = await r.json().catch(() => ({}));
  // ⚠ СЮДА НЕ ПИШЕМ. `badResponses` — это то, на что пожаловалась СТРАНИЦА; отказы, которые
  // проверка вызывает НАМЕРЕННО (анонимное чтение, слабый пароль, чужой прогон), — её собственные
  // действия, а не жалобы продукта. Первая редакция валила их в один список, и «контракты»
  // немедленно выродились в перечень оправданий для своих же шагов.
  return { status: r.status, body };
}

// Ожидание ПО СОСТОЯНИЮ, и потолок здесь СВОЙ. Гейт хаба ждёт 120 с прогона по локальной фикстуре;
// прогон, спрашивающий живую модель, — другой предмет, и наследовать чужое число вместе с чужим
// обоснованием ровно та ошибка, из-за которой в этом же дереве три проверки годами падали за
// восьмисотмиллисекундным сном. Число ниже ЗАМЕРЕНО на стенде разработчика (см. тело PR).
async function waitForRun(token, id, capMs) {
  const t0 = Date.now();
  for (;;) {
    const r = await api(token, `/v1/runs/${id}`);
    const state = r.body && r.body.state;
    if (state && state !== 'running') return { state, ms: Date.now() - t0 };
    if (Date.now() - t0 > capMs) {
      // Печатаем СОДЕРЖИМОЕ, а не «таймаут»: слепое ожидание даёт слепой диагноз.
      throw new Error(`прогон ${id} не дошёл до исхода за ${capMs} мс; последнее состояние ` +
        `${JSON.stringify(state)}, ответ ${JSON.stringify(r.body).slice(0, 300)}`);
    }
    await new Promise((r2) => setTimeout(r2, 500));
  }
}

async function main() {
  await bringUp();
  browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => { pageErrors.push(e.message); consoleErrors.push(`pageerror: ${e.message}`); });
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(`console: ${m.text()}`); });
  page.on('response', (r) => { if (r.status() >= 400) badResponses.push(`${r.status()} ${r.url()}`); });

  let adminPass = '';
  let adminTok = '';
  // ⚠ ПОТОЛОК ALICE ВЫБРАН НИЖЕ ЕСТЕСТВЕННОЙ ДЛИНЫ ОБХОДА, И ЭТО НЕ ВКУС. Замерено на фикстуре
  // testdata/site: обход сходится на ДВУХ шагах — и эвристикой, и живым qwen3:8b. При потолках 2 и 3
  // оба плана выходили длиной 2, то есть числа производил обход, а не настройка, и утверждение
  // «личный потолок доехал» было бы вакуумным ровно тогда, когда доставка сломана. Потолок 1 бьёт
  // раньше обхода, поэтому единица в плане может появиться ТОЛЬКО из настройки этого аккаунта.
  const users = [
    { name: 'mu-alice', pass: 'mu-alice-pass1', steps: '1', tok: '', runID: '' },
    { name: 'mu-bob', pass: 'mu-bob-pass1', steps: '3', tok: '', runID: '' },
  ];

  try {
    await check('развёртывание не начинается с пустой двери: пароль первого администратора напечатан РОВНО один раз', async () => {
      const rx = /created the first administrator "([^"]+)" with a generated password: (\S+)/g;
      const hits = [...log.matchAll(rx)];
      ok(hits.length > 0, `в выводе развёртывания нет строки о первом администраторе:\n${log.slice(0, 1200)}`);
      eq(hits.length, 1, 'строка о пароле первого администратора напечатана не один раз');
      eq(hits[0][1], 'admin', 'первый администратор назван не так, как обещает развёртывание');
      adminPass = hits[0][2];
      ok(adminPass.length >= 12, `сгенерированный пароль подозрительно короток (${adminPass.length} знаков)`);
      // Пароль НЕ уходит ни в снимки, ни в отчёт, ни в служебный журнал: он печатается один раз и
      // намеренно нигде больше не хранится.
      ok(!/generated password/.test(JSON.stringify(results)), 'пароль просочился в отчёт');
    });

    await check('сгенерированным паролем можно войти, и сервер называет вошедшего', async () => {
      const r = await api(null, '/v1/login', { method: 'POST', body: JSON.stringify({ name: 'admin', password: adminPass }) });
      eq(r.status, 200, `вход администратора отвергнут: ${JSON.stringify(r.body).slice(0, 200)}`);
      ok(r.body.session, 'вход прошёл без выданной сессии');
      adminTok = r.body.session;
      eq(r.body.user && r.body.user.is_admin, true, 'первый аккаунт заведён НЕ администратором');
      const me = await api(adminTok, '/v1/me');
      eq(me.status, 200, 'сессия не признаётся сразу после выдачи');
      eq(me.body.machine, false, 'человеческая сессия выдана за машинную');
      eq(me.body.scoped, true, 'сессия человека не ограничена владельцем');
      eq(me.body.user && me.body.user.name, 'admin', '/v1/me называет не того, кто вошёл');
    });

    await check('первый аккаунт закрыл анонимное чтение', async () => {
      const r = await api(null, '/v1/runs');
      eq(r.status, 403, 'список прогонов отдаётся БЕЗ кредентиала при заведённых аккаунтах');
    });

    await check('администратор заводит остальных, и пустой пароль не принимается', async () => {
      for (const u of users) {
        const r = await api(adminTok, '/v1/users', {
          method: 'POST', body: JSON.stringify({ name: u.name, password: u.pass, is_admin: false }),
        });
        eq(r.status, 201, `POST /v1/users ${u.name}: ${JSON.stringify(r.body).slice(0, 200)}`);
      }
      const weak = await api(adminTok, '/v1/users', {
        method: 'POST', body: JSON.stringify({ name: 'mu-weak', password: 'x', is_admin: false }),
      });
      ok(weak.status >= 400, 'аккаунт с односимвольным паролем заведён без возражений');
      const list = await api(adminTok, '/v1/users');
      eq(list.status, 200, 'список аккаунтов недоступен администратору');
      const n = (list.body.users || []).length;
      ok(n >= MIN_ACCOUNTS, `аккаунтов ${n}, ожидалось не меньше ${MIN_ACCOUNTS} (пол)`);
    });

    await check('каждый входит и получает СВОЮ сессию', async () => {
      for (const u of users) {
        const r = await api(null, '/v1/login', { method: 'POST', body: JSON.stringify({ name: u.name, password: u.pass }) });
        eq(r.status, 200, `вход ${u.name} отвергнут: ${JSON.stringify(r.body).slice(0, 200)}`);
        u.tok = r.body.session;
        const me = await api(u.tok, '/v1/me');
        eq(me.body.user && me.body.user.name, u.name, `сессия ${u.name} называет другого`);
        eq(me.body.user.is_admin, false, `${u.name} получил права администратора`);
      }
      ok(users[0].tok !== users[1].tok, 'двум аккаунтам выдана одна и та же сессия');
    });

    await check('каждый меняет СВОИ настройки прогона и не может тронуть настройки развёртывания', async () => {
      for (const u of users) {
        const put = await api(u.tok, '/v1/config', { method: 'PUT', body: JSON.stringify({ run: { max_steps: u.steps } }) });
        eq(put.status, 200, `${u.name} не смог сохранить свою секцию run: ${JSON.stringify(put.body).slice(0, 200)}`);
        const got = await api(u.tok, '/v1/config');
        eq(got.status, 200, `${u.name} не может прочитать свой конфиг`);
        eq(got.body.sources && got.body.sources.run, 'user', `секция run у ${u.name} числится не за ним`);
        eq(got.body.may_write_global, false, `обычному аккаунту ${u.name} разрешена запись настроек развёртывания`);
        eq(got.body.config && got.body.config.run && String(got.body.config.run.max_steps), u.steps,
          `${u.name} прочитал не своё значение max_steps`);
      }
      // ОБРАТНОЕ УТВЕРЖДЕНИЕ, и без него первое ничего не стоит: «может писать своё» и «не может
      // писать чужое» — разные свойства, и регрессия ломает их поодиночке.
      const refused = await api(users[0].tok, '/v1/config', { method: 'PUT', body: JSON.stringify({ llm: { model: 'anything' } }) });
      eq(refused.status, 403, 'обычный аккаунт записал настройки развёртывания');
      ok(refused.body.refused_sections || refused.body.error,
        'отказ не называет, КАКАЯ секция отвергнута — вызывателю остаётся делить свой документ пополам');
      // И ЛИЧНЫЕ НАСТРОЙКИ ДВУХ ЛЮДЕЙ НЕ ОДНО И ТО ЖЕ.
      const a = await api(users[0].tok, '/v1/config');
      const b = await api(users[1].tok, '/v1/config');
      ok(String(a.body.config.run.max_steps) !== String(b.body.config.run.max_steps),
        'два аккаунта читают ОДНУ личную настройку — личный слой не разделён');
    });

    // ── прогоны ──────────────────────────────────────────────────────────────────────────────────
    const runBody = (u) => {
      const b = { target: TARGET, mode: 'explore', max_steps: u.steps };
      // ⚠ max_steps шлём ПОЛЕМ. Личная секция `run` — это умолчания формы, а не окружение прогона:
      // в окружение материализуются только глобальные секции (cmd/control-api/logenv.go).
      // Проверка «поставил себе и прогон стал короче» без этого поля провалилась бы, и провал
      // назвал бы изоляцию, а не доставку.
      if (LLM_BASE) {
        // ⚠ В объекте `llm` НЕТ ключа `model` — только model_planner/model_heal
        // (cmd/control-api/llmenv.go). Ключ `model` был бы принят и молча не применён.
        b.planner = 'llm';
        b.llm = { backend: 'openai', base_url: LLM_BASE, model_planner: LLM_MODEL, model_heal: LLM_MODEL };
      }
      return b;
    };

    await check('каждый заводит СВОЙ прогон и тот доходит до исхода', async () => {
      for (const u of users) {
        const r = await api(u.tok, '/v1/runs', { method: 'POST', body: JSON.stringify(runBody(u)) });
        eq(r.status, 202, `прогон ${u.name} не принят: ${JSON.stringify(r.body).slice(0, 300)}`);
        ok(r.body.run_id, `прогон ${u.name} принят без run_id`);
        u.runID = r.body.run_id;
      }
      for (const u of users) {
        const { state, ms } = await waitForRun(u.tok, u.runID, LLM_BASE ? 600_000 : 180_000);
        console.log(`       прогон ${u.name}: ${state} за ${Math.round(ms / 1000)} с`);
        ok(state !== 'running', `прогон ${u.name} остался в состоянии running`);
      }
      const n = users.filter((u) => u.runID).length;
      ok(n >= MIN_OWN_RUNS, `прогонов ${n}, ожидалось не меньше ${MIN_OWN_RUNS} (пол)`);
    });

    // ⚠ ЭТО И ЕСТЬ ПРОВЕРКА «каждый меняет СВОИ настройки прогона» — предыдущая мерила только
    // круговорот документа. Круговорот сохранённого значения ничего не говорит о том, ДОЕХАЛО ли
    // оно: замерено, что личная секция `run` в окружение прогона не материализуется вовсе, и
    // настройка действует ровно потому, что интерфейс шлёт её полем тела. Утверждение сверяет
    // потолок с НЕЗАВИСИМЫМ наблюдением — числом шагов в замороженном плане, — а не повторяет
    // формулу доставки.
    //
    // ⚠ ПОТОЛОК — ЭТО ПРЕДЕЛ, А НЕ ЦЕЛЬ, и первая редакция этого не различала. Она требовала, чтобы
    // у двоих планы были РАЗНОЙ длины при потолках 2 и 3 — верно для эвристики, которая обходит
    // фикстуру детерминированно, и ложно для живой модели, которая сошлась на двух шагах у обоих.
    // Проверка прошла офлайн и упала на первом же живом прогоне: она утверждала свойство одного
    // планировщика как свойство продукта. Теперь утверждается ровно то, что делает потолок:
    // аккаунт с потолком НИЖЕ естественной длины обхода обрезан своим числом, а у соседа с большим
    // потолком шагов строго больше — то есть потолки персональны, а не общие.
    await check('личный потолок шагов ДОЕХАЛ до прогона и ОБРЕЗАЛ его', async () => {
      const stepsOf = (u) => {
        const p = path.join(REPO, 'runs', `control-${u.runID}`, 'plan.json');
        ok(fs.existsSync(p), `у прогона ${u.name} нет замороженного плана (${p})`);
        const plan = JSON.parse(fs.readFileSync(p, 'utf8'));
        ok(Array.isArray(plan.steps), `план ${u.name} не содержит шагов`);
        return plan.steps.length;
      };
      const counts = users.map((u) => { const n = stepsOf(u); console.log(`       ${u.name}: max_steps=${u.steps}, шагов в плане ${n}`); return n; });
      for (let i = 0; i < users.length; i++) {
        ok(counts[i] > 0, `план ${users[i].name} пуст — прогон не сделал ничего, и сравнение потолков было бы вакуумным`);
        ok(counts[i] <= Number(users[i].steps),
          `${users[i].name} просил потолок ${users[i].steps}, а план несёт ${counts[i]} шагов — потолок не доехал вовсе`);
      }
      // ОБРЕЗАННЫЙ аккаунт: его число может произвести только его собственная настройка — обход сам
      // по себе даёт больше (замерено: два шага обоими планировщиками).
      eq(counts[0], Number(users[0].steps),
        `${users[0].name} с потолком ${users[0].steps} получил ${counts[0]} шагов; потолок ниже длины обхода обязан дать РОВНО себя, ` +
        'иначе до прогона доехало умолчание развёртывания, а не настройка аккаунта');
      ok(counts[1] > counts[0],
        `у ${users[1].name} (потолок ${users[1].steps}) шагов ${counts[1]}, у ${users[0].name} (потолок ${users[0].steps}) — ${counts[0]}; ` +
        'числа обязаны отличаться, иначе потолок один на всех, а не персональный');
    });

    if (LLM_BASE) {
      await check('прогон СПРОСИЛ живую модель, а не откатился на эвристику', async () => {
        // ⚠ ФОРМА ЗАПИСИ ЗАМЕРЕНА, А НЕ ВСПОМНЕНА: {"step":N,"planner":"heuristic"|"llm",
        // "model":null|"…","prompt_tokens":null|N,"completion_tokens":null|N}. Первая редакция
        // искала `tokens_in`/`input_tokens` — имена, которых в этом дереве нет, — и утверждение
        // проходило бы по одному лишь слову `planner`, то есть ровно тем способом, каким прогон
        // однажды уже отрапортовал успех, не спросив модель.
        //
        // ⚠ АККАУНТ С ПОТОЛКОМ 1 ИЗ ЭТОГО УТВЕРЖДЕНИЯ ИСКЛЮЧЁН, И ВОТ ПОЧЕМУ. Замерено живьём:
        // прогон, обрезанный на первом шаге, пишет
        //   {"step":1,"planner":"llm","model":"qwen3:8b","decision":"done","reason":"max_steps",
        //    "prompt_tokens":null,"completion_tokens":null}
        // — то есть НАЗЫВАЕТ планировщик и модель, не спросив ни того ни другого: шаг ушёл на
        // навигацию, решать было нечего. Это самостоятельная находка: слово `planner: "llm"` в
        // журнале НЕ доказывает обращения, доказывают только ненулевые токены. Соседняя проверка
        // потолка требует от одного аккаунта потолок НИЖЕ длины обхода, поэтому два утверждения
        // хотят от него противоположного — и правильный ответ не «ослабить», а взять предметом те
        // аккаунты, у которых решение вообще возникает.
        const asked = users.filter((u) => Number(u.steps) > 1);
        ok(asked.length > 0,
          'ни у одного аккаунта потолок не допускает решения (все ≤ 1) — утверждение о живой модели ' +
          'стало бы вакуумным: обрезанный прогон называет планировщик, ничего не спросив');
        for (const u of asked) {
          const p = path.join(REPO, 'runs', `control-${u.runID}`, 'llm-transcript.jsonl');
          ok(fs.existsSync(p), `у прогона ${u.name} нет журнала обращений к модели (${p}) — планировщик молча остался эвристикой`);
          const lines = fs.readFileSync(p, 'utf8').split('\n').filter(Boolean)
            .map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
          ok(lines.length > 0, `журнал модели у ${u.name} пуст`);
          const llm = lines.filter((l) => l.planner === 'llm');
          ok(llm.length > 0,
            `в журнале ${u.name} нет НИ ОДНОЙ записи с planner: "llm" — прогон отчитался успехом, НЕ СПРОСИВ модель. ` +
            `Первая запись: ${JSON.stringify(lines[0]).slice(0, 300)}`);
          const tokens = llm.reduce((s, l) => s + (Number(l.prompt_tokens) || 0) + (Number(l.completion_tokens) || 0), 0);
          ok(tokens > 0,
            `у ${u.name} ${llm.length} записей planner: "llm" и НОЛЬ токенов — слово есть, обращения нет: ` +
            JSON.stringify(llm[0]).slice(0, 300));
          const models = [...new Set(llm.map((l) => l.model).filter(Boolean))];
          ok(models.length > 0, `у ${u.name} ни одна запись не называет модель`);
          console.log(`       ${u.name}: ${llm.length} обращений к ${models.join(', ')}, токенов ${tokens}`);
        }
      });
    } else {
      declaredSkip('прогон СПРОСИЛ живую модель, а не откатился на эвристику',
        'живой модели на этом стенде нет — эндпоинт живёт в локальной сети мейнтейнера и с раннера ' +
        'недостижим (docs/PR_ACCEPTANCE.md §2). Запустите с --llm-base, чтобы проверка исполнилась.');
    }

    await check('изоляция по API: чужой прогон невидим — включая АДМИНИСТРАТОРА', async () => {
      const [a, b] = users;
      const own = await api(a.tok, '/v1/runs');
      ok((own.body.runs || []).some((r) => r.run_id === a.runID), 'владелец не видит собственного прогона');
      const other = await api(b.tok, '/v1/runs');
      ok(!(other.body.runs || []).some((r) => r.run_id === a.runID),
        'другой аккаунт видит чужой прогон в списке — скоупинг регрессировал');
      const adm = await api(adminTok, '/v1/runs');
      ok(!(adm.body.runs || []).some((r) => r.run_id === a.runID),
        'АДМИНИСТРАТОР видит чужой прогон в списке — скоупинг регрессировал');
      eq((await api(adminTok, `/v1/runs/${a.runID}`)).status, 404, 'администратор достал чужой прогон по адресу');
      eq((await api(adminTok, `/v1/runs/${a.runID}/artifact?name=scenario.json`)).status, 404, 'администратор достал чужой артефакт');
      eq((await api(a.tok, `/v1/runs/${a.runID}`)).status, 200, 'владелец потерял доступ к собственному прогону');
    });

    await check('и ПРОТИВОПОЛОЖНОЕ свойство: технический журнал администратор видит целиком', async () => {
      const admLog = await api(adminTok, '/v1/service-log?limit=200');
      const usrLog = await api(users[0].tok, '/v1/service-log?limit=200');
      eq(admLog.body.scoped, false, 'служебный журнал ОГРАНИЧЕН для администратора — аудиторская половина исчезла');
      eq(usrLog.body.scoped, true, 'служебный журнал НЕ ограничен для обычного аккаунта — он видит всё развёртывание');
      ok((admLog.body.matched || 0) > (usrLog.body.matched || 0),
        `администратор видит ${admLog.body.matched} записей, обычный — ${usrLog.body.matched}; ` +
        'числа обязаны отличаться, иначе одно из двух правил скоупинга ничего не делает');
    });

    // ── глазами интерфейса ───────────────────────────────────────────────────────────────────────
    await page.goto(base(), { waitUntil: 'load' });
    await page.waitForTimeout(400);
    await shot(page, 'hub-loaded');

    const openSettings = async () => { await page.click('.rail a[data-nav="settings"]'); await page.waitForTimeout(200); };
    // Вход ЖДЁТ СОСТОЯНИЕ, а не интервал, и признак берётся у ДЕЙСТВИЯ: `#id-status` пишет только
    // обработчик клика, тогда как `#id-who` перерисовывается собственным idRefresh() страницы и
    // удовлетворяет ожидание, пока запрос ещё в полёте.
    const signIn = async (name, password) => {
      await openSettings();
      await page.fill('#id-name', name);
      await page.fill('#id-pass', password);
      await page.click('#id-login');
      await page.waitForFunction(() => {
        const st = document.getElementById('id-status');
        return !!st && /[✓✗]/.test(st.textContent || '');
      }, null, { timeout: 30_000 });
      if (/✓/.test(await page.locator('#id-status').innerText())) {
        await page.locator('#id-logout').waitFor({ state: 'visible', timeout: 15_000 });
      }
    };
    const openRuns = async () => {
      await page.click('.rail a[data-nav="library"]');
      await page.click('[data-innerbar="library"] .subtab-btn[data-sub="runs"]');
      await page.click('#runs-refresh');
      await page.waitForFunction(() => {
        const e = document.getElementById('runs-list');
        return !!e && !/загрузка|loading/i.test(e.innerHTML);
      }, null, { timeout: 20_000 });
    };

    await check('интерфейс пускает человека по логину и говорит, кто работает', async () => {
      // ⚠ Признак входа — НЕ «подключился». Обработчик #cap-check ходит только на открытый
      // /healthz, поэтому «подключено» зелено и с мусорным токеном; сказать «вошёл как такой-то»
      // может только идентичность.
      await signIn(users[0].name, users[0].pass);
      const who = await page.locator('#id-who').innerText();
      ok(new RegExp(users[0].name).test(who), `страница не называет вошедшего: ${JSON.stringify(who)}`);
      ok(await page.locator('#id-logout').isVisible(), 'нет способа выйти после входа');
      await shot(page, 'signed-in-as-user');
    });

    await check('изоляция ГЛАЗАМИ хаба: свой прогон виден, чужой — нет, и администратору тоже', async () => {
      try {
        await openRuns();
        ok(await page.locator(`#runs-list [data-rerun="${users[0].runID}"]`).count(),
          `хаб не показывает владельцу его собственный прогон; список: ${(await page.locator('#runs-list').innerText()).slice(0, 300)}`);
        ok(!(await page.locator(`#runs-list [data-rerun="${users[1].runID}"]`).count()),
          'хаб показывает одному аккаунту прогон ДРУГОГО');
        await shot(page, 'runs-as-owner', true);

        await openSettings();
        await signIn('admin', adminPass);
        await openRuns();
        ok(!(await page.locator(`#runs-list [data-rerun="${users[0].runID}"]`).count()),
          'хаб показывает АДМИНИСТРАТОРУ прогон, принадлежащий другому аккаунту');
        await shot(page, 'runs-as-admin', true);
      } finally {
        // ⚠ ВОССТАНОВЛЕНИЕ СТЕНДА — В finally. Упавшая проверка иначе оставляет вход под чужим
        // аккаунтом и на чужой вкладке, и СЛЕДУЮЩАЯ падает по причине, к ней не относящейся.
        await openSettings().catch(() => {});
      }
    });

    await check('каждый раздел хаба снят под живым развёртыванием, и перечень ВЫВЕДЕН из разметки', async () => {
      const views = hubViews();
      ok(views.length >= MIN_VIEWS, `видов ${views.length}, ожидалось не меньше ${MIN_VIEWS} (пол)`);
      for (const v of views) {
        await page.click(`.rail a[data-nav="${v}"]`).catch(() => {});
        await page.waitForTimeout(250);
        await shot(page, `view-${v}`, true);
      }
    });

    await check('браузер не жаловался, пока стенд проходили насквозь', async () => {
      // Контракты — с ПРИЧИНОЙ у каждого; допущение без записанной причины превращает гейт в
      // список оправданий.
      // Контракты — каждый с ПРИЧИНОЙ. Допущение без записанной причины превращает гейт в перечень
      // оправданий; поэтому их ровно три и каждое названо.
      const contract = (s) => /^503 .*\/readyz/.test(s)   // развёртывание без модели отвечает 503 навсегда — это продукт, а не отказ
        || /^40[13] /.test(s)                              // API отказывает хабу, который ещё не вошёл — ровно то, ради чего заведена личность
        || /^404 .*\/artifact\?name=/.test(s)              // хаб СПРАШИВАЕТ, есть ли необязательный артефакт; 404 — это ответ «нет»
        || /^404 .*\/v1\/config$/.test(s);                 // конфиг ещё не записан ничем: развёртывание, которое только что поднялось
      const noise = /Failed to load resource/;
      const defects = [
        ...consoleErrors.filter((e) => !noise.test(e)),
        ...badResponses.filter((r) => !contract(r)),
      ];
      const seen = [...new Set([...consoleErrors, ...badResponses])];
      if (seen.length) fs.writeFileSync(path.join(OUT, 'browser-errors.txt'), seen.join('\n') + '\n');
      console.log(`       браузер и API дали ${seen.length} строк(и); дефектов среди них ${defects.length}`);
      ok(defects.length === 0, `${defects.length} дефект(ов):\n       ${[...new Set(defects)].slice(0, 8).join('\n       ')}`);
    });
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (capi) capi.kill('SIGTERM');
    if (gw) gw.kill('SIGTERM');
    // ⚠ УБОРКА БАЗЫ, КОТОРОЙ НЕТ У ГЕЙТА ХАБА. Тот шлёт только SIGTERM, и замерено, что за волны
    // так накопилось 259 файлов `state/hub-gate-<pid>.db` на 31 МБ. Сокет шлюз снимает сам, база —
    // ничья, поэтому её снимаем здесь.
    await new Promise((r) => setTimeout(r, 300));
    for (const s of ['', '-shm', '-wal']) { try { fs.unlinkSync(storeDB + s); } catch { /* уже нет */ } }
    try { fs.unlinkSync(storeSock); } catch { /* шлюз снял сам */ }
  }

  // ⚠ РАВЕНСТВО, а не потолок: пропуск ровно один и только без --llm-base.
  const wantSkips = LLM_BASE ? 0 : DECLARED_SKIPS_WITHOUT_MODEL;
  if (skips.length !== wantSkips) {
    console.log(`\nОБЪЯВЛЕННЫХ ПРОПУСКОВ ${skips.length} при ожидаемых ${wantSkips} — ` +
      'пропуск заводится вместе с причиной и вместе с этим числом, иначе следующий пройдёт молча');
    skips.forEach((s) => console.log(`  · ${s.name}: ${s.reason}`));
    process.exit(1);
  }
  if (skips.length) { console.log('\nОбъявленные пропуски:'); skips.forEach((s) => console.log(`  · ${s.name}\n    ${s.reason}`)); }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  console.log(`${shotN} screenshots in ${OUT}`);
  process.exit(failed.length ? 1 : 0);
}

main().catch(async (e) => {
  console.error(e);
  if (browser) await browser.close().catch(() => {});
  if (capi) capi.kill('SIGTERM');
  if (gw) gw.kill('SIGTERM');
  for (const s of ['', '-shm', '-wal']) { try { fs.unlinkSync(storeDB + s); } catch { /* уже нет */ } }
  try { fs.unlinkSync(storeSock); } catch { /* уже нет */ }
  console.log(`\nстенд не поднялся или прогон оборвался; накопленный вывод развёртывания:\n${log.slice(0, 2000)}`);
  process.exit(1);
});
