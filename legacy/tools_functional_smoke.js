const { chromium } = (() => {
  try {
    return require('playwright');
  } catch (err) {
    return require('/usr/lib/node_modules/playwright');
  }
})();

const BASE_URL = 'http://alexmediaserver.ddns.net:8081/';
const AUTH = { user: 'admin', pass: 'admin1234' };

function short(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

async function loginIfNeeded(page, notes) {
  if (await page.locator('#auth-user').isVisible().catch(() => false)) {
    await page.fill('#auth-user', AUTH.user);
    await page.fill('#auth-pass', AUTH.pass);
    await page.click('#auth-btn');
    await page.waitForFunction(() => {
      const auth = document.getElementById('auth-screen');
      const app = document.getElementById('app');
      return !!app && app.style.display !== 'none' && !!auth && !auth.classList.contains('visible');
    }, null, { timeout: 15000 });
    notes.push('login:ok');
  } else {
    notes.push('login:session-active');
  }
}

async function expect(condition, message, failures) {
  if (!condition) failures.push(message);
}

async function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function runDesktop(page, failures, notes) {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto(BASE_URL, { waitUntil: 'networkidle', timeout: 30000 });
  await loginIfNeeded(page, notes);

  await page.click('.nav-item[data-view="carte"]');
  await page.waitForTimeout(1200);

  await expect(await page.locator('#map-toolbar-float').isVisible(), 'desktop: toolbar carte absente', failures);
  await expect(await page.locator('.year-select').isVisible().catch(() => false), 'desktop: selecteur de campagne absent sur la carte', failures);

  await page.click('.topbar-search-btn');
  await page.waitForTimeout(150);
  await expect((await page.locator('#search-overlay').count()) === 1, 'desktop: recherche ne s’ouvre pas proprement', failures);
  await page.fill('#search-input', 'ilot');
  await page.waitForTimeout(300);
  const resultCount = await page.locator('.search-result').count();
  await expect(resultCount > 0, 'desktop: aucun resultat de recherche pour "ilot"', failures);
  if (resultCount > 0) {
    await page.locator('.search-result').first().click();
    await page.waitForTimeout(400);
    await expect((await page.locator('#search-overlay').count()) === 0, 'desktop: overlay de recherche non fermee apres clic resultat', failures);
  } else {
    await page.click('#search-overlay', { position: { x: 20, y: 20 } }).catch(() => {});
  }

  const searchKeyboard = await page.evaluate(() => {
    closeSearch();
    openSearch();
    openSearch();
    const countAfterDoubleOpen = document.querySelectorAll('#search-overlay').length;
    const escEvent = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true });
    document.dispatchEvent(escEvent);
    return {
      countAfterDoubleOpen,
      countAfterEscape: document.querySelectorAll('#search-overlay').length,
    };
  });
  notes.push(`search-double-open:${searchKeyboard.countAfterDoubleOpen}`);
  notes.push(`search-after-escape:${searchKeyboard.countAfterEscape}`);
  await expect(searchKeyboard.countAfterDoubleOpen === 1, 'desktop: la recherche peut s’ouvrir en double', failures);
  await expect(searchKeyboard.countAfterEscape === 0, 'desktop: Echap ne ferme pas la recherche', failures);
  await page.evaluate(() => { if (typeof closeSearch === 'function') closeSearch(); });
  await page.waitForTimeout(150);

  const satBefore = await page.evaluate(() => ({
    label: String(document.querySelector('#map-toolbar-float #sat-btn')?.textContent || '').replace(/\s+/g, ' ').trim(),
    url: window.APP && APP.tileLayer ? APP.tileLayer._url : '',
  }));
  await page.click('#map-toolbar-float #sat-btn');
  await page.waitForTimeout(400);
  const satAfter = await page.evaluate(() => ({
    label: String(document.querySelector('#map-toolbar-float #sat-btn')?.textContent || '').replace(/\s+/g, ' ').trim(),
    url: window.APP && APP.tileLayer ? APP.tileLayer._url : '',
  }));
  notes.push(`sat-before:${satBefore.label}`);
  notes.push(`sat-after:${satAfter.label}`);
  await expect(satBefore.url !== satAfter.url, 'desktop: bascule carte/satellite inactive', failures);
  await expect(satBefore.label !== satAfter.label, 'desktop: libelle carte/satellite inchange', failures);

  await page.click('#map-toolbar-float #draw-poly-btn');
  await page.waitForTimeout(200);
  const drawLabelStart = await page.locator('#map-toolbar-float #draw-poly-btn').innerText();
  await page.click('#map-toolbar-float #draw-poly-btn');
  await page.waitForTimeout(200);
  const drawLabelStop = await page.locator('#map-toolbar-float #draw-poly-btn').innerText();
  notes.push(`draw-start:${short(drawLabelStart)}`);
  notes.push(`draw-stop:${short(drawLabelStop)}`);
  await expect(/arre/i.test(drawLabelStart), 'desktop: le bouton dessin n’entre pas en mode arret', failures);
  await expect(/dessin/i.test(drawLabelStop), 'desktop: le bouton dessin ne revient pas a l’etat initial', failures);
}

async function runMobile(page, failures, notes) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE_URL, { waitUntil: 'networkidle', timeout: 30000 });
  await loginIfNeeded(page, notes);

  await page.click('#mobile-more-btn');
  await page.waitForTimeout(200);
  await expect(await page.locator('#mobile-more-menu.open').isVisible().catch(() => false), 'mobile: menu Plus ne s’ouvre pas', failures);

  await page.click('.mobile-more-item[data-view="rapports"]');
  await page.waitForTimeout(600);
  const mobileState = await page.evaluate(() => ({
    view: window.APP ? APP.view : '',
    moreOpen: document.getElementById('mobile-more-menu')?.classList.contains('open') || false,
    moreActive: document.getElementById('mobile-more-btn')?.classList.contains('active') || false,
  }));
  notes.push(`mobile-view:${mobileState.view}`);
  await expect(mobileState.view === 'rapports', 'mobile: navigation via menu Plus ko', failures);
  await expect(!mobileState.moreOpen, 'mobile: menu Plus reste ouvert apres navigation', failures);
  await expect(mobileState.moreActive, 'mobile: bouton Plus non actif sur une vue secondaire', failures);

  await page.click('.hamburger');
  await page.waitForTimeout(150);
  await expect(await page.locator('.sidebar.open').isVisible().catch(() => false), 'mobile: sidebar ne s’ouvre pas', failures);
  await page.click('#sidebar-overlay', { position: { x: 360, y: 180 } });
  await page.waitForTimeout(150);
  const sidebarStillOpen = await page.locator('.sidebar.open').count();
  await expect(sidebarStillOpen === 0, 'mobile: sidebar ne se ferme pas via overlay', failures);

  const mobileSearch = await page.evaluate(() => {
    const el = document.querySelector('.topbar-search-mobile');
    if (!el) return { exists: false };
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return {
      exists: true,
      display: style.display,
      visibility: style.visibility,
      width: rect.width,
      height: rect.height,
    };
  });
  notes.push(`mobile-search:${JSON.stringify(mobileSearch)}`);
  if (!mobileSearch.exists || mobileSearch.display === 'none' || mobileSearch.visibility === 'hidden' || !mobileSearch.width || !mobileSearch.height) {
    failures.push('mobile: bouton recherche absent ou non visible');
  } else {
    await page.click('.topbar-search-mobile');
    await page.waitForTimeout(150);
    await expect((await page.locator('#search-overlay').count()) === 1, 'mobile: recherche ne s’ouvre pas', failures);
    await page.click('#search-overlay', { position: { x: 16, y: 16 } });
    await page.waitForTimeout(150);
    await expect((await page.locator('#search-overlay').count()) === 0, 'mobile: recherche ne se ferme pas au clic exterieur', failures);
  }
}

(async () => {
  const failures = [];
  const notes = [];
  const consoleErrors = [];
  const pageErrors = [];
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  page.on('console', msg => {
    if (['error', 'warning'].includes(msg.type())) {
      consoleErrors.push(`${msg.type()}: ${short(msg.text())}`);
    }
  });
  page.on('pageerror', err => pageErrors.push(short(err && err.message)));

  try {
    await runDesktop(page, failures, notes);
    await runMobile(page, failures, notes);
  } finally {
    await page.screenshot({ path: '/tmp/agristat-functional-smoke.png', fullPage: true }).catch(() => {});
    await browser.close();
  }

  const report = {
    ok: failures.length === 0 && pageErrors.length === 0,
    failures,
    notes,
    consoleErrors,
    pageErrors,
  };

  console.log(JSON.stringify(report, null, 2));
  if (!report.ok) process.exit(1);
})().catch(err => {
  console.error(JSON.stringify({ ok: false, fatal: short(err && err.stack || err) }, null, 2));
  process.exit(1);
});
