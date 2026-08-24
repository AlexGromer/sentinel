/**
 * ADR-134 — `browser.click` ЗАМЕЧАЕТ навигацию, а не угадывает её по мгновенному снимку адреса.
 *
 * ЧТО ЭТО ЗАКРЫВАЕТ. До ADR-134 обработчик читал `page.url()` тем же тактом, что и клик, и отдавал
 * его как есть. Для навигации ДОКУМЕНТА этого хватало. Для SPA — нет, и вдвойне: Playwright
 * обновляет адрес фрейма по протокольному событию (`Page.navigatedWithinDocument`), поэтому даже
 * СИНХРОННЫЙ `history.pushState` может не успеть отразиться, а роутер, откладывающий переход
 * (Angular, ленивый чанк), меняет адрес через несколько тиков — и возвращался СТАРЫЙ. Обход, для
 * которого клик и есть способ найти маршрут, тихо не замечал находку.
 *
 * ПОЧЕМУ ЭТОТ ГЕЙТ ГОНЯЕТ НАСТОЯЩИЙ БРАУЗЕР. Утверждается свойство ГОНКИ между чужим кодом и нашим
 * чтением. Любая дешёвая форма проверяет что-то другое: что в исходнике есть слово `waitForURL`,
 * что функция вызвана. Этот репозиторий уже замерил, чего такие утверждения стоят. Поэтому гейт
 * говорит с отгружаемым исполнителем по его же JSON-RPC — ровно как brain — и читает результат
 * вербами, которые уже есть. Ни одной вербы ради теста не заведено.
 *
 * Каждая проверка называет мутацию, ради которой существует.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';

const SERVER = path.join(__dirname, 'server.js');
const REPO = path.resolve(__dirname, '..', '..');
const FIXTURE = pathToFileURL(path.join(REPO, 'testdata', 'fixtures', 'l13-routes.html')).href;

/** Тот же минимальный JSON-RPC клиент, что в decorate.test.ts: протокол brain/executor.py. */
class Exec {
  private proc: ChildProcess;
  private buf = '';
  private waiting = new Map<number, { ok: (v: unknown) => void; bad: (e: Error) => void }>();
  private nextId = 1;

  constructor(env: Record<string, string> = {}) {
    this.proc = spawn(process.execPath, [SERVER], {
      env: { ...process.env, ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.proc.stdout!.setEncoding('utf8');
    this.proc.stdout!.on('data', (chunk: string) => {
      this.buf += chunk;
      for (let i = this.buf.indexOf('\n'); i >= 0; i = this.buf.indexOf('\n')) {
        const line = this.buf.slice(0, i).trim();
        this.buf = this.buf.slice(i + 1);
        if (!line) continue;
        const msg = JSON.parse(line) as { id: number; result?: unknown; error?: { message: string } };
        const w = this.waiting.get(msg.id);
        if (!w) continue;
        this.waiting.delete(msg.id);
        if (msg.error) w.bad(new Error(msg.error.message));
        else w.ok(msg.result);
      }
    });
    this.proc.stderr!.setEncoding('utf8');
    this.proc.stderr!.resume();
  }

  call<T = Record<string, unknown>>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.waiting.set(id, { ok: (v) => resolve(v as T), bad: reject });
      this.proc.stdin!.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  async close(): Promise<void> {
    // ⚠ ОЖИДАНИЕ `shutdown` ОГРАНИЧЕНО, и это не перестраховка. Исполнитель, повисший внутри
    // верба (ровно то, что проверяет гейт про потолок 0), на `shutdown` не ответит НИКОГДА —
    // а `await` без потолка превращает быстрый провал в зависший сьют. Замерено: мутация
    // «убрать проверку `> 0`» вешала `npm test` на десять минут вместо отказа за восемь секунд.
    // Провал обязан быть быстрым, иначе его перестают воспроизводить.
    try {
      await Promise.race([
        this.call('shutdown'),
        new Promise((r) => { const t = setTimeout(r, 2000); t.unref(); }),
      ]);
    } catch { /* already down */ }
    await new Promise<void>((done) => {
      const t = setTimeout(done, 5000);
      t.unref();
      this.proc.once('exit', () => { clearTimeout(t); done(); });
    });
    this.proc.kill('SIGKILL');
  }
}

type ClickResult = { clicked: boolean; url: string; navigated?: boolean };

async function open(ex: Exec): Promise<void> {
  await ex.call('initialize');
  await ex.call('browser.navigate', { url: FIXTURE });
}

test('a click that changes the address is reported as a navigation — synchronously and late', async () => {
  // KILLS: `return { clicked: true, url: page!.url() }` — чтение адреса тем же тактом.
  // KILLS: отсутствие поля `navigated` — вызывающий сравнивал адреса сам и не отличал
  //        «не двигались» от «не успели увидеть».
  const ex = new Exec();
  try {
    await open(ex);

    const sync = await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    assert.equal(sync.navigated, true, 'синхронный pushState не признан навигацией');
    assert.match(sync.url, /#\/sync$/, `адрес после синхронного перехода: ${sync.url}`);

    // ⚠ ГЛАВНАЯ ПОЛОВИНА. Роутер меняет адрес через несколько тиков — клик уже вернулся.
    // Мгновенный снимок здесь возвращает ПРЕЖНИЙ адрес, и находка теряется молча.
    const late = await ex.call<ClickResult>('browser.click', { locator: { css: '#async' } });
    assert.equal(late.navigated, true, 'отложенный переход не замечен — клик вернулся раньше роутера');
    assert.match(late.url, /#\/async$/, `адрес после отложенного перехода: ${late.url}`);

    // Страница сама считает, сколько раз сменила маршрут, и публикует счёт в document.title —
    // канал, который доезжает по RPC и не трогает ни одного пикселя (приём из l11-decorate.html).
    const seen = await ex.call<{ url: string; title: string }>('browser.currentUrl');
    assert.match(seen.title, /routes=2/, `страница насчитала не два перехода: ${seen.title}`);
  } finally {
    await ex.close();
  }
});

test('a click that changes nothing is NOT reported as a navigation', async () => {
  // Встречное утверждение, и без него первое удовлетворяется кодом, объявляющим навигацией
  // КАЖДЫЙ клик. KILLS: `navigated: true` константой.
  const ex = new Exec();
  try {
    await open(ex);
    const before = await ex.call<{ url: string }>('browser.currentUrl');
    const inert = await ex.call<ClickResult>('browser.click', { locator: { css: '#inert' } });
    assert.equal(inert.navigated, false, 'клик, не тронувший адрес, объявлен навигацией');
    assert.equal(inert.url, before.url, 'адрес изменился там, где страница его не меняла');
  } finally {
    await ex.close();
  }
});

test('a settle bound of zero means DO NOT WAIT, not wait forever', async () => {
  // ⚠ ЗАМЕРЕННЫЙ ДЕФЕКТ, А НЕ ГИПОТЕЗА. В Playwright `timeout: 0` СНИМАЕТ потолок, поэтому первая
  // редакция на `SENTINEL_CLICK_NAV_SETTLE_MS=0` вешала прогон на ПЕРВОМ клике без навигации:
  // ожидание адреса, который никогда не сменится, длилось вечно. Замерено — прогон встал на шаге 2
  // и не двинулся, и нашла это не проверка, а попытка замерить цену ожидания через A/B с нулём.
  // Ноль — самое естественное значение для «выключить», и оно обязано выключать.
  // KILLS: `{ timeout: CLICK_NAV_SETTLE_MS }` без проверки `> 0`.
  const ex = new Exec({ SENTINEL_CLICK_NAV_SETTLE_MS: '0' });
  try {
    await open(ex);
    const done = await Promise.race([
      ex.call<ClickResult>('browser.click', { locator: { css: '#inert' } }).then(() => 'returned'),
      new Promise<string>((r) => { const t = setTimeout(() => r('hung'), 8000); t.unref(); }),
    ]);
    assert.equal(done, 'returned',
      'клик без навигации при потолке 0 не вернулся за 8 с — ноль снял потолок вместо того, чтобы ' +
      'выключить ожидание, и прогон повис бы на первом же клике');
  } finally {
    await ex.close();
  }
});

test('the settle bound is a ceiling, not a delay: a navigating click does not pay it', async () => {
  // ⚠ ЦЕНА ОЖИДАНИЯ ЗАМЕРЕНА, А НЕ ПРЕДПОЛОЖЕНА. `waitForURL` с предикатом разрешается немедленно,
  // если адрес уже сменился, поэтому потолок платит ТОЛЬКО клик, который навигацией не был.
  // Потолок здесь поднят до 2000 мс намеренно: с дефолтными 250 разница потонула бы в шуме, а
  // утверждение стало бы про скорость машины, а не про механизм.
  // KILLS: `await page.waitForTimeout(CLICK_NAV_SETTLE_MS)` вместо ожидания состояния.
  const ex = new Exec({ SENTINEL_CLICK_NAV_SETTLE_MS: '2000' });
  try {
    await open(ex);
    // ⚠ ИМЕННО ОТЛОЖЕННЫЙ переход, а не синхронный. Первая редакция мерила `#sync`, и мутация
    // «заменить ожидание состояния сном» ПРОШЛА зелёной: к моменту проверки адрес уже успевал
    // смениться, ветка ожидания не бралась вовсе, и сон не оплачивался. Разница между ожиданием и
    // сном видна ровно там, где ветка ожидания БЕРЁТСЯ.
    const t0 = Date.now();
    await ex.call<ClickResult>('browser.click', { locator: { css: '#async' } });
    const navigating = Date.now() - t0;
    const t1 = Date.now();
    await ex.call<ClickResult>('browser.click', { locator: { css: '#inert' } });
    const inert = Date.now() - t1;

    assert.ok(navigating < 1500,
      `клик-навигация ждала ${navigating} мс при потолке 2000 — ожидание превратилось в сон`);
    assert.ok(inert >= 1800,
      `клик БЕЗ навигации занял ${inert} мс при потолке 2000 — потолок не применяется, и значит ` +
      'отложенный переход не дождался бы своего адреса');
  } finally {
    await ex.close();
  }
});
