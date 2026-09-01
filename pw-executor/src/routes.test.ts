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

// --- W8 PR-2 (ADR-135): журнал маршрутов ----------------------------------------------------------
//
// ⚠ ЭТОТ ФАЙЛ — ЕДИНСТВЕННОЕ, ЧТО ВООБЩЕ МОЖЕТ ПРОВЕРИТЬ СТРАНИЧНУЮ ПОЛОВИНУ. Init-скрипт живёт в
// строке (`addInitScript({content})` иного не принимает), а строку `tsc` не разбирает и линтера в
// пакете нет ни одного. Синтаксическая ошибка внутри неё собирается ЗЕЛЁНОЙ и проявляется только в
// браузере — то есть в прогоне пользователя. Поэтому проверка тут и говорит с настоящим Chromium.

type Take = { routes: Array<{ url: string; ts: number; how: string }>; dropped: number; journal: boolean };

test('the journal records what the address snapshot cannot see: a redirect chain', async () => {
  // ⚠ ГЛАВНОЕ УТВЕРЖДЕНИЕ PR-2, и оно ПАРНОЕ. Кнопка делает два pushState в ОДНОМ такте: между ними
  // снаружи нет ни одного протокольного обмена, поэтому `page.url()` — сколько его ни жди — вернёт
  // только конечный адрес. Промежуточный маршрут существует ровно в журнале.
  // KILLS: журнал, читающий адрес по событию Playwright вместо перехвата в странице.
  // KILLS: любая редакция, отдающая только последнюю запись.
  const ex = new Exec();
  try {
    await open(ex);
    await ex.call<Take>('browser.routes');                       // сбросить след загрузки
    const click = await ex.call<ClickResult>('browser.click', { locator: { css: '#chain' } });
    const took = await ex.call<Take>('browser.routes');
    const urls = took.routes.map((r) => r.url);

    assert.match(click.url, /#\/chain\/final$/,
      `снаружи виден только конечный адрес — это и есть предпосылка: ${click.url}`);
    assert.ok(urls.some((u) => /#\/chain\/guard$/.test(u)),
      `ПРОМЕЖУТОЧНЫЙ маршрут не попал в журнал (${JSON.stringify(urls)}) — значит журнал видит то ` +
      'же, что снимок адреса, и заводить его было незачем');
    assert.ok(urls.some((u) => /#\/chain\/final$/.test(u)),
      `конечный маршрут не попал в журнал: ${JSON.stringify(urls)}`);
  } finally {
    await ex.close();
  }
});

test('replaceState and back are recorded, and each says which mechanism moved the address', async () => {
  // `replaceState` не поднимает НИ ОДНОГО события и не оставляет следа в истории: единственный
  // способ узнать о ней — обёртка. Возврат — вторая половина: он тоже смена маршрута.
  // KILLS: обёртка только над pushState (так было до PR-2 и в реплике офлайн-замера).
  // KILLS: отсутствие подписки на popstate.
  const ex = new Exec();
  try {
    await open(ex);
    await ex.call<Take>('browser.routes');

    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    await ex.call<ClickResult>('browser.click', { locator: { css: '#replace' } });
    const afterReplace = await ex.call<Take>('browser.routes');
    const hows = afterReplace.routes.map((r) => r.how);
    assert.ok(hows.includes('push'), `нет записи push: ${JSON.stringify(afterReplace.routes)}`);
    assert.ok(hows.includes('replace'),
      `replaceState не записан (${JSON.stringify(afterReplace.routes)}) — обёртка стоит только над ` +
      'pushState, и редиректы роутера остаются невидимыми');

    await ex.call<ClickResult>('browser.click', { locator: { css: '#back' } });
    const afterBack = await ex.call<Take>('browser.routes');
    assert.ok(afterBack.routes.some((r) => r.how === 'pop'),
      `возврат не записан: ${JSON.stringify(afterBack.routes)}`);
  } finally {
    await ex.close();
  }
});

test('a fragment reached by clicking an anchor is recorded — the measurement that removed hashchange', async () => {
  // ⚠ ЭТА ПРОВЕРКА ДЕРЖИТ ЗАМЕР, А НЕ ПОВЕДЕНИЕ НАШЕГО КОДА. Замерено 2026-08-24: в Chromium клик по
  // `<a href="#/x">`, присвоение `location.hash` и `history.back()` поднимают `popstate` И
  // `hashchange`, причём popstate ВСЕГДА первым. Поэтому отдельный слушатель `hashchange` не мог бы
  // выиграть отсечку повтора ни разу, и значение `how: "hash"` было бы мёртвой альтернативой —
  // именем, которое читатель артефакта принял бы за нечто, что бывает. Слушателя нет; если движок
  // это изменит, красным станет ЭТА строка, а не тишина в журнале.
  const ex = new Exec();
  try {
    await open(ex);
    await ex.call<Take>('browser.routes');
    await ex.call<ClickResult>('browser.click', { locator: { css: '#anchor' } });
    const took = await ex.call<Take>('browser.routes');
    assert.ok(took.routes.some((r) => /#\/from-anchor$/.test(r.url)),
      `переход по якорю-фрагменту не записан (${JSON.stringify(took.routes)}) — Chromium перестал ` +
      'поднимать popstate на фрагмент, и слушателя hashchange, который это закрывал, здесь нет');
  } finally {
    await ex.close();
  }
});

test('the journal survives navigation, and taking it clears it', async () => {
  // Две половины одного механизма, и обе — ловушки, названные в исходнике.
  // KILLS: `page.evaluate` вместо `context.addInitScript` — журнал пропал бы на первой навигации.
  // KILLS: чтение без очистки — журнал отдавал бы всё с начала прогона на каждом шаге, ворота
  //        отсеивали бы это молча по ADMIT_KNOWN, а стоимость росла бы квадратично.
  const ex = new Exec();
  try {
    await open(ex);
    await ex.call<Take>('browser.routes');
    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    const first = await ex.call<Take>('browser.routes');
    assert.ok(first.routes.length >= 1, 'до навигации журнал уже пуст');

    const again = await ex.call<Take>('browser.routes');
    assert.equal(again.routes.length, 0,
      `повторное чтение на неподвижной странице отдало ${again.routes.length} запис(ей) — журнал ` +
      'читают и не чистят');

    // Навигация ДОКУМЕНТА: одноразовый evaluate здесь бы и стёрся.
    await ex.call('browser.navigate', { url: FIXTURE });
    await ex.call<Take>('browser.routes');
    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    const afterNav = await ex.call<Take>('browser.routes');
    assert.equal(afterNav.journal, true, 'после навигации журнала на документе нет — инъекция потеряна');
    assert.ok(afterNav.routes.length >= 1,
      'после навигации документа журнал не записал ничего — скрипт не переустановился');
  } finally {
    await ex.close();
  }
});

test('the journal is bounded, and what it dropped is counted rather than silently lost', async () => {
  // ⚠ ПОТОЛОК УТВЕРЖДАЕТСЯ ПОВЕДЕНЧЕСКИ, А НЕ ЧТЕНИЕМ ИСХОДНИКА. «В коде есть срез массива» —
  // суррогат: мутация проходит насквозь. Заливаем ЗАВЕДОМО больше потолка и требуем двух вещей
  // сразу: журнал остался ограниченным И разница НАЗВАНА. Без второй половины переполнение теряет
  // находки молча, а молчаливая потеря — ровно то, чего этот журнал не имеет права делать.
  //
  // ⚠ ЧИСЛО ЗАЛИВКИ ВЫВОДИТСЯ ИЗ ПОТОЛКА И ПЕРЕДАЁТСЯ ФИКСТУРЕ ЗАПРОСОМ. Записать его в фикстуру
  // значило бы завести ВТОРОЕ число того же смысла: подняли потолок — фикстура молча перестала его
  // достигать, и проверка стала бы зелёной над отсутствующим механизмом.
  // KILLS: удаление проверки `log.length >= MAX`.
  // KILLS: переполнение, не считающее отброшенное (`dropped` остался нулём).
  const { ROUTE_JOURNAL_MAX } = await import('./routes.js');
  const extra = 7;
  const ex = new Exec();
  try {
    await ex.call('initialize');
    await ex.call('browser.navigate', { url: `${FIXTURE}?flood=${ROUTE_JOURNAL_MAX + extra}` });
    await ex.call<Take>('browser.routes');
    await ex.call<ClickResult>('browser.click', { locator: { css: '#flood' } });
    const took = await ex.call<Take>('browser.routes');

    assert.equal(took.routes.length, ROUTE_JOURNAL_MAX,
      `журнал держит ${took.routes.length} записей при потолке ${ROUTE_JOURNAL_MAX} — потолка нет ` +
      'либо он не тот, что объявлен');
    assert.equal(took.dropped, extra,
      `отброшено ${took.dropped} при заливке на ${extra} сверх потолка — переполнение теряет ` +
      'записи молча, и человек прочитает неполный журнал как полный');
  } finally {
    await ex.close();
  }
});

test('a route reopened right after a drain is recorded — the drain closes the dedup window', async () => {
  // ADR-142. Дедуп подряд идущих повторов (`last`) переживал слив: он продолжал сравнивать с
  // записью из ПРЕДЫДУЩЕГО окна, которого у читателя журнала уже нет. Замер до правки: клик #sync,
  // слив отдал одну запись; клик #sync ЕЩЁ РАЗ, слив отдал ПУСТО — возврат на тот же адрес сразу
  // после слива исчезал бесследно. Это противоречило контракту, записанному в шапке routes.ts:
  // «A → B → A остаётся тремя записями — это разные моменты, и обход обязан видеть возврат».
  // KILLS: `last`, переживающий take() (та самая редакция, что стояла до ADR-142).
  const ex = new Exec();
  try {
    await open(ex);
    await ex.call<Take>('browser.routes');                        // сбросить след загрузки

    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    const first = await ex.call<Take>('browser.routes');
    assert.ok(first.routes.some((r) => /#\/sync$/.test(r.url)),
      `предпосылка не выполнена — первый переход не записан: ${JSON.stringify(first.routes)}`);

    // Тот же адрес ещё раз, уже в НОВОМ окне наблюдения. Для приложения это отдельный момент.
    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    const second = await ex.call<Take>('browser.routes');
    assert.ok(second.routes.some((r) => /#\/sync$/.test(r.url)),
      `возврат на тот же адрес сразу после слива не записан (${JSON.stringify(second.routes)}) — ` +
      'дедуп сравнивает с записью из окна, которое уже отдано и очищено');
  } finally {
    await ex.close();
  }
});

test('inside ONE window a repeated address is still collapsed — the ceiling guard survives', async () => {
  // Парная половина к проверке выше, и она существует ровно затем, чтобы починка не была сделана
  // ценой того, ради чего `last` заведён: `replaceState` на каждый чих не имеет права выбирать
  // потолок журнала копиями одного адреса.
  // KILLS: снятие дедупа целиком (например, удаление проверки `href === last`).
  const ex = new Exec();
  try {
    await open(ex);
    await ex.call<Take>('browser.routes');

    // Три перехода на ОДИН адрес без слива между ними.
    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    await ex.call<ClickResult>('browser.click', { locator: { css: '#sync' } });
    const took = await ex.call<Take>('browser.routes');

    const sync = took.routes.filter((r) => /#\/sync$/.test(r.url));
    assert.equal(sync.length, 1,
      `внутри одного окна адрес записан ${sync.length} раз(а) — дедуп снят, и приложение, ` +
      `синхронизирующее адрес через replaceState, выберет потолок журнала копиями: ` +
      JSON.stringify(took.routes));
  } finally {
    await ex.close();
  }
});
