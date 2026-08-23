// Ф4 — НАЖАТЬ каждый контрол хаба и посмотреть, что произойдёт.
//
// Запуск:  node scripts/ui-press.mjs --base http://127.0.0.1:8090 --token <tok> [--out <dir>]
//
// ЗАЧЕМ ЭТО ОТДЕЛЬНО ОТ ui-smoke. Тот ОПИСЫВАЕТ и ФОТОГРАФИРУЕТ каждый контрол, а нажимает четыре:
// `#cap-check`, `#b-run` дважды и одно переключение вида. Проверено обходом scripts/ui-smoke.mjs.
// То есть «протыкать каждую кнопку» не было покрыто ничем — и в тот же день, когда протыкивание
// впервые довели до конца, оно нашло дефект, который весь CI пропускал: копирующая кнопка объявляла
// «скопировано», не дождавшись промиса и не имея обработчика отказа, — в буфере при этом лежал
// NotAllowedError. Кадр этого не показывал (ярлык выглядел правильным), гейты не звали.
//
// ⚠ ПЕРЕЧЕНЬ ВЫВОДИТСЯ, А НЕ ИЩЕТСЯ ПО ИМЕНИ. Обход помечает атрибутом ровно те элементы, которые
// считает инвентарь ui-smoke — тем же селектором `button, summary, [role="button"]` и с той же
// дедупликацией по элементу, — и кликает по метке. Три редакции этого инструмента ошиблись именно
// здесь, и каждая ошибка выглядела как дефект продукта:
//
//   поиск по getByRole('button')  → 21 контрол «не найден»: все <summary>, которых роль button не
//                                   покрывает. Дефект инструмента, отрапортованный как дефект хаба.
//   без раскрытия <details>       → 53 контрола «невидим»: лежат в свёрнутых блоках.
//   сопоставление по НОМЕРУ       → «исчез после предыдущих нажатий» на контроле, который никуда не
//                                   девался: нажатие внутренней вкладки переставляет видимые
//                                   элементы, и элемент под номером i становится другим.
//
// Отсюда ключ вместо номера (тег + имя + номер вхождения среди одноимённых) и перепометка ПЕРЕД
// каждым кликом: hzRender() и его родня переписывают innerHTML целиком, и метки исчезают вместе с
// поддеревом. Молчаливо промахнуться мимо исчезнувшей метки дороже, чем перепометить.
//
// ⚠ ЧТО НЕ НАЖИМАЕТСЯ — ОБЪЯВЛЕНО, А НЕ УМОЛЧАНО. Форма списана с `componentsWithoutProbe`
// (cmd/control-api/readyz.go), где отсутствие пробы обязано быть записанным решением: пропуск без
// причины — это омиссия, научившаяся проходить гейт. Две категории: разрушительные (нажать «удалить»
// на живом стенде значит уничтожить состояние, которым меряют) и требующие СЦЕНАРИЯ (идущего
// прогона, открытого артефакта, выбранного режима) — этот обход их не создаёт.
import { createRequire } from 'node:module';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// Тот же приём, что у ui-smoke: playwright живёт в pw-executor/node_modules, а не рядом со скриптом.
const require = createRequire(path.join(REPO, 'pw-executor', 'package.json'));
const { chromium } = require('playwright');

const arg = (n, d) => {
  const i = process.argv.indexOf(`--${n}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : d;
};
const BASE = arg('base', 'http://127.0.0.1:8090');
const TOKEN = arg('token', '');
const OUT = arg('out', path.join(REPO, 'ui-press'));
const JOURNAL = path.join(REPO, 'state', 'logs', 'service.jsonl');

// Пол на число НАЖАТЫХ. Обход, переставший что-либо находить, пройдёт идеально над пустым множеством
// — это единственное, чего сам вывод перечня не ловит. Замерено 2026-08-22: 73 нажатия из 85
// контролов. Пол может только расти.
const MIN_PRESSED = 65;

// Разрушительное определяется по ФОРМЕ ярлыка, а не по намерению автора: слово, по которому человек
// понимает, что кнопка что-то уничтожит.
const DESTRUCTIVE = /удал|очист|purge|delete|drop|sweep|выйти|sign ?out|logout|revoke|сброс|reset/i;

// Контролы, которые этот обход НЕ нажимает, каждый с записанной причиной. Ключ — подстрока ярлыка
// или id. Записи здесь обязаны ИСЧЕЗАТЬ: потолок ниже равен их числу и двигается только вместе с
// ними, поэтому «разрешить ещё один» нельзя, не написав, что именно разрешается.
const NOT_PRESSED = {
  'НастройкиSettings': 'секция co-pilot (`#copilot`, ADR-055) несёт data-view="__never" — вида нет ' +
    'в VIEWS, роутер его не покажет никогда, и `display:none` стоит на секции постоянно. Нажимать ' +
    'нечего; расхождение перечней (инвентарь идёт по [data-view], роутер по VIEWS) заведено как ' +
    '[INVENTORY-COUNTS-UNREACHABLE-VIEW]',
  'ТестыTests': 'та же секция `#copilot`, та же причина',
  '?ПодсказкаHelp': 'единственная из семнадцати «Подсказок», лежащая в `#b-descWrap` — блоке режима ' +
    'describe, скрытом, пока режим не выбран. Остальные шестнадцать нажимаются',
  'stop the running run': '`#ch-stop` показывается только пока прогон ИДЁТ. Обход прогонов не ' +
    'запускает: нажать «остановить» на живом прогоне значит уничтожить то, что меряют',
  '✕': '`#art-view` — просмотрщик артефактов, скрыт, пока артефакт не открыт',
};
// ПОТОЛОК, и он может только опускаться. Число, которое умеет расти, — это список оправданий с
// гейтом при нём (та же механика, что у MAX_UNPROBED в tests/test_readyz_covers_the_stack_offline.py).
const MAX_NOT_PRESSED = 5;

const journalLines = () => {
  try { return fs.readFileSync(JOURNAL, 'utf8').split('\n').filter(Boolean).length; } catch { return 0; }
};
const journalTail = (from) => {
  try {
    return fs.readFileSync(JOURNAL, 'utf8').split('\n').filter(Boolean).slice(from)
      .map((l) => { try { return JSON.parse(l).code; } catch { return null; } }).filter(Boolean);
  } catch { return []; }
};

const failures = [];
const fail = (m) => failures.push(m);

// markView помечает контролы вида ровно тем обходом, каким их считает ui-smoke.
const markView = (page, view) =>
  page.evaluate((v) => {
    const name = (el) => (el.getAttribute('aria-label') || el.getAttribute('title')
      || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    const seen = new Set();
    const labels = [];
    const occ = new Map();
    for (const sec of document.querySelectorAll(`[data-view="${v}"]`)) {
      for (const b of sec.querySelectorAll('button, summary, [role="button"]')) {
        if (seen.has(b)) continue;
        seen.add(b);
        const nm = name(b);
        const base = b.tagName.toLowerCase() + '|' + (b.id || nm);
        const n = occ.get(base) || 0;
        occ.set(base, n + 1);
        const key = base + '|' + n;
        b.setAttribute('data-press-key', key);
        labels.push({ key, tag: b.tagName.toLowerCase(), name: nm, id: b.id || '' });
      }
    }
    return labels;
  }, view);

// openDetails раскрывает свёрнутые блоки, чтобы их содержимое было ДОСТИЖИМО. Это не подмена клика:
// сами <summary> остаются в перечне и нажимаются наравне со всеми — раскрытие лишь снимает то, что
// иначе засчиталось бы как «невидим» и не проверялось бы вовсе.
const openDetails = (page, view) =>
  page.evaluate((v) => {
    for (const sec of document.querySelectorAll(`[data-view="${v}"]`)) {
      for (const d of sec.querySelectorAll('details')) d.open = true;
    }
  }, view);

const views = async (page) => page.evaluate(() =>
  [...new Set([...document.querySelectorAll('[data-view]')]
    .flatMap((e) => e.getAttribute('data-view').split(' ')))].filter(Boolean));

const browser = await chromium.launch({ headless: true });
// Разрешение на буфер обмена НЕ выдаётся намеренно: так выглядит первый визит обычного человека, и
// именно в этом состоянии копирующая кнопка врала.
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();
// ⚠ ДВА РАЗНЫХ ЖУРНАЛА, потому что это две разные новости.
//
//   broken  — исключение JavaScript или НЕОБРАБОТАННОЕ отклонение промиса. Это сломанный код, и
//             гейт обязан на нём краснеть: именно в таком виде пришёл дефект копирующей кнопки.
//   netLog  — «Failed to load resource» с кодом сервера. Это НЕ обязательно дефект: нажатие
//             «Создать» с пустой формой законно получает 400, и продукт показывает человеку
//             «✗ name is required» — замерено. Валить на этом значило бы завести детектор,
//             срабатывающий на законный контент, а такие в этом репозитории удаляют.
//             Числа выводятся в отчёт: неожиданный код увидит человек, а не молчание.
const broken = [];
const netLog = [];
page.on('pageerror', (e) => broken.push(String(e).slice(0, 160)));
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const t = m.text().slice(0, 200);
  (/^Failed to load resource/.test(t) ? netLog : broken).push(t);
});

await page.goto(BASE, { waitUntil: 'domcontentloaded' });

// ⚠ ПОДКЛЮЧЕНИЕ ДОВОДИТСЯ ДО СОСТОЯНИЯ, А НЕ ДО СНА. Редакция, которая заполняла поля и спала
// полторы секунды, не подключалась вовсе — и КАЖДЫЙ нажатый контрол ловил 403. Обход рапортовал
// «ошибка в консоли» на семнадцати кнопках, а причиной был он сам. Шаги те же, что у ui-smoke:
// подключение одно, и второй способ его выполнять был бы вторым высказыванием об одном факте.
await page.evaluate(() => { location.hash = '#v=settings'; });
await page.waitForSelector('#capi', { state: 'visible', timeout: 15000 });
await page.fill('#capi', BASE);
if (TOKEN) await page.fill('#capitok', TOKEN);
await page.click('#cap-check');
await page.waitForFunction(
  () => (document.getElementById('cap-status') || {}).textContent?.includes('ok'),
  undefined, { timeout: 20000 });

const rows = [];
const tally = { pressed: 0, destructive: 0, disabled: 0, declared: 0, undeclared: 0, errored: 0, netnoise: 0, journalled: 0 };
let total = 0;

for (const view of await views(page)) {
  await page.evaluate((v) => { location.hash = `#v=${v}`; }, view);
  await page.waitForTimeout(280);
  await openDetails(page, view);
  const labels = await markView(page, view);
  total += labels.length;

  for (const lab of labels) {
    const label = lab.name || lab.id || '(без имени)';
    const declaredFor = Object.keys(NOT_PRESSED).find((k) => label.includes(k) || lab.id === k);

    if (DESTRUCTIVE.test(label) || DESTRUCTIVE.test(lab.id)) {
      rows.push({ view, control: label, tag: lab.tag, result: 'не нажат — разрушительный', codes: [] });
      tally.destructive += 1;
      continue;
    }

    // Состояние восстанавливается перед КАЖДЫМ кликом: предыдущий мог увести на другой вид, свернуть
    // блок или перерисовать панель.
    await page.evaluate((v) => { location.hash = `#v=${v}`; }, view);
    await page.waitForTimeout(110);
    await openDetails(page, view);
    let fresh = await markView(page, view);
    if (!fresh.some((f) => f.key === lab.key)) {
      const why = declaredFor ? `не нажат — ОБЪЯВЛЕНО: ${NOT_PRESSED[declaredFor]}` : 'НЕ НАЖАТ — исчез после предыдущих нажатий';
      if (declaredFor) tally.declared += 1; else { tally.undeclared += 1; fail(`${view} / ${label}: исчез под обходом и не объявлен`); }
      rows.push({ view, control: label, tag: lab.tag, result: why, codes: [] });
      continue;
    }

    const loc = page.locator(`[data-view="${view}"] [data-press-key=${JSON.stringify(lab.key)}]`).first();
    const before = journalLines();
    const errsBefore = broken.length;
    const netBefore = netLog.length;
    let result = 'нажат';
    try {
      if (await loc.isDisabled().catch(() => false)) {
        rows.push({ view, control: label, tag: lab.tag, result: 'не нажат — отключён в этом состоянии', codes: [] });
        tally.disabled += 1;
        continue;
      }
      // Контрол в НЕАКТИВНОЙ подпанели скрыт не поломкой, а выбором вкладки. Вкладка открывается
      // НАЖАТИЕМ — законным путём пользователя, — а не подменой состояния из скрипта: подмена
      // проверила бы отрисовку и пропустила бы саму вкладку, которая тоже контрол и тоже в перечне.
      if (!(await loc.isVisible().catch(() => false))) {
        const sub = await page.evaluate(({ v, k }) => {
          const el = document.querySelector(`[data-view="${v}"] [data-press-key="${k.replace(/"/g, '\\"')}"]`);
          for (let n = el; n && n !== document.body; n = n.parentElement) {
            const p = n.getAttribute && n.getAttribute('data-subpanel');
            if (p) return p;
          }
          return null;
        }, { v: view, k: lab.key });
        if (sub) {
          await page.click(`.subtab-btn[data-sub="${sub}"]`, { timeout: 1200 }).catch(() => {});
          await page.waitForTimeout(180);
          await openDetails(page, view);
        }
      }
      if (!(await loc.isVisible().catch(() => false))) {
        // ⚠ ИНСТРУМЕНТ ОБЯЗАН НАЗВАТЬ, ЧТО ИМЕННО ПРЯЧЕТ КОНТРОЛ. «Timeout 2000ms» — это симптом, и
        // под ним лежали РАЗНЫЕ вещи: постоянно скрытая секция, блок другого режима, просмотрщик
        // артефактов. Догадываться о механизме нельзя; надо спросить страницу.
        const why = await page.evaluate(({ v, k }) => {
          const el = document.querySelector(`[data-view="${v}"] [data-press-key="${k.replace(/"/g, '\\"')}"]`);
          if (!el) return 'элемента нет в DOM';
          for (let n = el; n && n !== document.body; n = n.parentElement) {
            const cs = getComputedStyle(n);
            if (cs.display === 'none' || cs.visibility === 'hidden' || n.hidden) {
              const id = n.id ? '#' + n.id : '';
              return `скрыт предком <${n.tagName.toLowerCase()}${id}>`;
            }
          }
          const r = el.getBoundingClientRect();
          return r.width === 0 || r.height === 0 ? `нулевой размер (${r.width}×${r.height})` : 'видим по стилям, но клик невозможен';
        }, { v: view, k: lab.key });
        if (declaredFor) {
          tally.declared += 1;
          rows.push({ view, control: label, tag: lab.tag, result: `не нажат — ОБЪЯВЛЕНО (${why}): ${NOT_PRESSED[declaredFor]}`, codes: [] });
        } else {
          tally.undeclared += 1;
          fail(`${view} / ${label}: ${why} — контрол не нажат и причина НЕ ОБЪЯВЛЕНА. Либо доведи ` +
               `обход до состояния, в котором он виден, либо впиши его в NOT_PRESSED с причиной; ` +
               `непроверенное, посчитанное проверенным, — это то, ради чего этот обход написан`);
          rows.push({ view, control: label, tag: lab.tag, result: `НЕ НАЖАТ — ${why}`, codes: [] });
        }
        continue;
      }
      await loc.scrollIntoViewIfNeeded({ timeout: 1500 }).catch(() => {});
      // ⚠ ТАЙМАУТ ОБЯЗАТЕЛЕН: .catch() не отменяет ожидание — click без него ждёт дефолтные 30 с на
      // каждом непопавшем селекторе, и обход не заканчивается никогда. Замерено здесь же.
      await loc.click({ timeout: 2500 });
      tally.pressed += 1;
    } catch (e) {
      tally.undeclared += 1;
      result = 'НЕ НАЖАТ — клик не прошёл: ' + String(e.message || e).split('\n')[0].slice(0, 90);
      fail(`${view} / ${label}: ${result}`);
    }
    await page.waitForTimeout(200);
    const codes = journalTail(before);
    if (codes.length) tally.journalled += 1;
    const newBroken = broken.length - errsBefore;
    const newNet = netLog.length - netBefore;
    if (newBroken > 0) {
      tally.errored += 1;
      const detail = broken.slice(errsBefore).join(' | ').slice(0, 220);
      result += ` ⚠ СЛОМАННЫЙ КОД: ${newBroken} — ${detail}`;
      fail(`${view} / ${label}: нажатие вызвало исключение или необработанное отклонение промиса ` +
           `(${newBroken}) — ${detail}. Именно так выглядел дефект копирующей кнопки: обещание в ` +
           `ярлыке и отказ, уехавший в консоль`);
    }
    if (newNet > 0) {
      tally.netnoise += 1;
      result += ` · ответы сервера не-2xx: ${newNet} — ${netLog.slice(netBefore).join(' | ').slice(0, 160)}`;
    }
    rows.push({ view, control: label, tag: lab.tag, result, codes });
  }
}

fs.mkdirSync(OUT, { recursive: true });
fs.writeFileSync(path.join(OUT, 'presses.json'), JSON.stringify({ total, ...tally, rows }, null, 2));

console.log(`контролов в разметке: ${total}`);
console.log(`нажато: ${tally.pressed}`);
console.log(`не нажато с ОБЪЯВЛЕННОЙ причиной: ${tally.destructive} разрушительных · ${tally.disabled} отключённых · ${tally.declared} требующих сценария`);
console.log(`оставили след в журнале: ${tally.journalled}`);
console.log(`вызвали ИСКЛЮЧЕНИЕ в коде страницы: ${tally.errored}`);
console.log(`получили не-2xx от сервера (наблюдение, не провал): ${tally.netnoise}`);
if (tally.netnoise) {
  console.log('  какие именно:');
  for (const r of rows.filter((x) => /не-2xx/.test(x.result))) {
    console.log(`   ${r.view} / ${r.control} → ${r.result.split('· ответы сервера ')[1] || ''}`.slice(0, 180));
  }
}

if (tally.pressed < MIN_PRESSED) {
  fail(`нажато ${tally.pressed} контрол(ов) при поле ${MIN_PRESSED} — обход сузился, а сузившийся ` +
       `обход рапортует «всё чисто» ровно теми же словами, что и полный`);
}
if (MAX_NOT_PRESSED !== Object.keys(NOT_PRESSED).length) {
  fail(`MAX_NOT_PRESSED равен ${MAX_NOT_PRESSED} при ${Object.keys(NOT_PRESSED).length} записанных ` +
       `исключениях. Потолок — не бюджет, который можно потратить: это число уже принятых решений, ` +
       `и оно двигается только вместе с ними`);
}
for (const k of Object.keys(NOT_PRESSED)) {
  if (!rows.some((r) => r.control.includes(k) || r.control === k)) {
    fail(`NOT_PRESSED называет «${k}», которого в разметке больше нет — устаревшее исключение ` +
         `делает потолок теснее, чем он есть, и прикрывает собой ничего`);
  }
}

if (failures.length) {
  console.log(`\nFAIL — ${failures.length} проблем(а):`);
  for (const f of failures) console.log('  - ' + f);
  process.exit(1);
}
console.log(`\nOK — каждый контрол либо нажат, либо не нажат по записанной причине (${tally.pressed} + ` +
            `${tally.destructive + tally.disabled + tally.declared} = ${tally.pressed + tally.destructive + tally.disabled + tally.declared} из ${total})`);
await browser.close();
