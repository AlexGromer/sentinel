// ADR-142 — гейт на политику переинъекции рекордера. Runs under `node --import tsx --test`.
//
// ЗАЧЕМ ЭТОТ ФАЙЛ. Запись реестра [RECORDER-REINJECTS-ON-EVERY-ROUTE-CHANGE] несла оговорку
// «поведенческая правка требует своего гейта на политику service worker, а тестового каркаса для SW
// в проекте нет». Каркас и не понадобился: `index.ts` при импорте регистрирует слушателей на
// `chrome.*` и потому в jsdom не импортируется, но РЕШЕНИЕ («ставить или не ставить») от `chrome`
// не зависит вовсе. Оно вынесено в `ensure-recorder.ts` чистой функцией над двумя зависимостями, и
// проверяется здесь без единого мока браузерного API.
//
// ⚠ ЧЕГО ЭТОТ ФАЙЛ ДОКАЗАТЬ НЕ МОЖЕТ, сказано прямо. Он не проверяет, что `chrome.tabs.sendMessage`
// действительно отвергается, когда в вкладке нет получателя, — это свойство Chromium, а не наше.
// Оно ЗАМЕРЕНО (Chrome 151 / Chromium 150) и уже было заложено в `tellTab`, который ловил этот
// отказ с комментарием «no recorder in that tab yet». Здесь утверждается ровно наша половина: что
// из ответа и отказа делаются ПРАВИЛЬНЫЕ выводы.
import assert from 'node:assert/strict';
import test from 'node:test';
import { ensureRecorder, type EnsureDeps } from './ensure-recorder.js';

/** Счётчики + управляемый исход пинга. `alive` моделирует живой content-script. */
function tab(alive: boolean) {
  const calls = { ping: 0, inject: 0 };
  let live = alive;
  const deps: EnsureDeps = {
    ping: async (_id: number) => {
      calls.ping += 1;
      if (!live) throw new Error('Could not establish connection. Receiving end does not exist.');
      return undefined;
    },
    inject: async (_id: number) => {
      calls.inject += 1;
      live = true; // инъекция и делает вкладку отвечающей
    },
  };
  return { deps, calls };
}

test('a route change does NOT re-install the recorder — the live tab answers', async () => {
  // ГЛАВНОЕ УТВЕРЖДЕНИЕ. pushState не заменяет JS-контекст, content-script жив, и ставить нечего.
  // KILLS: безусловный injectRecorder в слушателе onUpdated (та редакция, что стояла до ADR-142).
  const { deps, calls } = tab(true);
  const outcome = await ensureRecorder(7, deps);
  assert.equal(outcome, 'alive');
  assert.equal(calls.inject, 0, 'живой рекордер переустановили — это и есть та самая трата');
  assert.equal(calls.ping, 1, 'пинг обязан быть ровно один: он же и доставляет record-control');
});

test('a burst of three route changes injects ZERO times, not three', async () => {
  // Замер из реестра назван поимённо: «на всплеске из трёх pushState инъекция идёт ТРИЖДЫ».
  // KILLS: любая редакция, ставящая по событию, а не по ответу вкладки.
  const { deps, calls } = tab(true);
  for (let i = 0; i < 3; i += 1) await ensureRecorder(7, deps);
  assert.equal(calls.inject, 0,
    `на трёх сменах маршрута сделано ${calls.inject} инъекций — на SPA с частым replaceState это ` +
    'сотни лишних executeScript за прогон, по два бандла каждый');
  assert.equal(calls.ping, 3);
});

test('a full document load DOES re-install — the case re-injection was written for', async () => {
  // Парная половина: экономия не имеет права съесть случай, ради которого слушатель существует.
  // KILLS: «никогда не ставить» (например, снятие ветки catch).
  const { deps, calls } = tab(false);
  const outcome = await ensureRecorder(7, deps);
  assert.equal(outcome, 'injected');
  assert.equal(calls.inject, 1, 'после замены документа рекордер не поставлен — запись прервалась');
});

test('after installing, the fresh recorder is told the state', async () => {
  // До инъекции сообщение слушать было некому, поэтому оно повторяется ПОСЛЕ — иначе свежий
  // рекордер стоит молча и не знает, что запись идёт.
  // KILLS: инъекция без последующего record-control.
  const { deps, calls } = tab(false);
  await ensureRecorder(7, deps);
  assert.equal(calls.ping, 2, 'состояние свежему рекордеру не сообщено — он не начнёт писать');
});

test('a tab that vanished mid-flight is not an error anyone has to hear about', async () => {
  // Вкладку закрыли между событием и инъекцией. Это не наша поломка; шуметь нечем и незачем.
  // KILLS: проброс исключения наружу (в SW это необработанный reject).
  const calls = { ping: 0, inject: 0 };
  const deps: EnsureDeps = {
    ping: async () => { calls.ping += 1; throw new Error('no receiver'); },
    inject: async () => { calls.inject += 1; throw new Error('No tab with id: 7.'); },
  };
  const outcome = await ensureRecorder(7, deps);
  assert.equal(outcome, 'gone');
  assert.equal(calls.inject, 1, 'попытка поставить обязана быть — иначе мы решили за браузер');
});
