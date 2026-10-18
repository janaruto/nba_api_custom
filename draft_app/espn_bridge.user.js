// ==UserScript==
// @name         Swiss See Ballers — ESPN draft bridge
// @namespace    local.draft-assistant
// @version      0.2
// @description  Reads the ESPN auction draft room in YOUR browser tab and sends nominations / sales to the local draft assistant (bridge.py). Read-only: it never clicks anything on ESPN.
// @match        https://fantasy.espn.com/basketball/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

/*
 * SETUP (Tampermonkey or Violentmonkey, in the browser where you are logged in to ESPN)
 *   1. python draft_app/bridge.py            -> prints where bridge_token.txt is
 *   2. Paste the token into CONFIG.TOKEN below.
 *   3. Open the draft room. The badge in the bottom-left corner shows "verbunden" and whether a draft room was found.
 *
 * The selectors were fitted on ESPN's practice draft for this league (2026-10-11): the room's own markup, not a guess.
 * If ESPN changes it, only the SELECTORS block and readRoom() need to be touched.
 *   Ctrl+Shift+D saves a snapshot of the page (scripts, styles, images stripped) to draft_app/draft_room_dump_<label>.html.
 *
 * This script only reads the page. It does not place bids and does not click anything.
 */

(function () {
  'use strict';

  const CONFIG = {
    BRIDGE: 'http://127.0.0.1:8765',
    TOKEN: 'PASTE_TOKEN_FROM_draft_app/bridge_token.txt',
    SCAN_MS: 700,           // how often the page is re-read besides on every DOM change
  };

  /*SELECTORS_BEGIN*/
  const SELECTORS = {
    // The player who is up for bidding right now (the panel above the player list).
    nomineeName: '.pickArea .player-selected .playerinfo__playername',
    currentBid: '.pickArea .current-amount',                 // "Current offer: $84"
    bidList: '.bid-history__list li.bid',                    // newest first: "$84 Undersized Problems"
    // The draft board holds ALL picks in the DOM (the "Picks" table is virtualised and only renders what is in view).
    // It is laid out column by column: one column per team (header cells), the same number of rows in each.
    boardHeader: '.draft-board-grid-header-cell',
    boardCells: '.draftBoardGrid > .draft-board-grid-pick-cell',
    doneClass: 'completedPick',
    cellFirst: '.playerFirstName',
    cellLast: '.playerLastName',
    cellPrice: '.winningPrice',
  };
  /*SELECTORS_END*/

  // ── helpers ──────────────────────────────────────────────────────────────────────────────────
  const clean = (t) => String(t || '').replace(/\s+/g, ' ').trim();
  const text = (root, sel) => {
    const el = sel ? root.querySelector(sel) : null;
    return el ? clean(el.textContent) : '';
  };
  const money = (t) => {
    const m = String(t || '').replace(/,/g, '').match(/\d+(\.\d+)?/);
    return m ? Math.round(parseFloat(m[0])) : null;
  };

  function post(path, obj, cb) {
    GM_xmlhttpRequest({
      method: 'POST',
      url: CONFIG.BRIDGE + path,
      headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': CONFIG.TOKEN },
      data: JSON.stringify(obj),
      onload: (r) => cb && cb(null, r),
      onerror: (e) => cb && cb(e || new Error('request failed')),
      ontimeout: () => cb && cb(new Error('timeout')),
    });
  }

  // ── what the room shows right now ─────────────────────────────────────────────────────────────
  const hasRoom = () => !!document.querySelector(SELECTORS.boardCells);

  function readRoom() {
    const cells = Array.from(document.querySelectorAll(SELECTORS.boardCells));
    if (!cells.length) return null;                                  // not on a draft-room page

    // nomination: the player in the bidding panel, his current offer, and who made it (newest bid in the history)
    let nomination = null;
    const name = text(document, SELECTORS.nomineeName);
    const offer = text(document, SELECTORS.currentBid);
    if (name && offer) {
      const top = text(document, SELECTORS.bidList);                 // first match = newest bid
      const team = top.replace(/^\$\s*[\d,]+\s*/, '').trim();
      nomination = { player: name, bid: money(offer), team: team || null };
    }

    // sales: every completed cell of the board, with the team taken from the column it sits in
    const headers = Array.from(document.querySelectorAll(SELECTORS.boardHeader)).map((h) => clean(h.textContent));
    const perCol = headers.length ? cells.length / headers.length : 0;
    const sold = [];
    if (Number.isInteger(perCol) && perCol > 0) {
      cells.forEach((cell, i) => {
        if (!cell.classList.contains(SELECTORS.doneClass)) return;
        const player = clean(text(cell, SELECTORS.cellFirst) + ' ' + text(cell, SELECTORS.cellLast));
        const price = money(text(cell, SELECTORS.cellPrice));
        const team = headers[Math.floor(i / perCol)];
        if (player && team && price) sold.push({ player, team, price });
      });
    }
    return { nomination, sold };
  }

  // ── send only what changed ────────────────────────────────────────────────────────────────────
  let lastNom = null;                 // key of the last nomination sent
  const sentSales = new Set();        // keys of sales already sent (the bridge deduplicates too)
  let sentCount = 0, failed = 0;

  function scan() {
    const room = readRoom();
    if (!room) return;
    const nomKey = room.nomination ? `${room.nomination.player}|${room.nomination.bid}|${room.nomination.team}` : null;
    if (nomKey !== lastNom) {
      lastNom = nomKey;
      const ev = room.nomination ? { type: 'nominated', ...room.nomination } : { type: 'clear' };
      post('/event', ev, (err) => { err ? failed++ : sentCount++; paint(); });
    }
    for (const s of room.sold) {
      const key = `${s.player}|${s.team}|${s.price}`;
      if (sentSales.has(key)) continue;
      sentSales.add(key);
      post('/event', { type: 'sold', ...s }, (err) => { err ? failed++ : sentCount++; paint(); });
    }
  }

  // ── snapshot, in case ESPN changes its markup ───────────────────────────────────────────────────
  function dump() {
    const label = window.prompt('Label für den Snapshot (z.B. nomination oder sold):', 'room');
    if (!label) return;
    const clone = document.body.cloneNode(true);
    clone.querySelectorAll('script, style, svg, img, video, canvas, link, noscript, iframe').forEach((n) => n.remove());
    const html = clone.outerHTML.replace(/\s+/g, ' ').slice(0, 5 * 1024 * 1024);
    post('/dump', { label, html }, (err) => {
      status = err ? 'Snapshot fehlgeschlagen' : `Snapshot gespeichert (${label})`;
      paint();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) { e.preventDefault(); dump(); }
  });

  // ── status badge ───────────────────────────────────────────────────────────────────────────
  let bridgeOk = false, status = '';
  const badge = document.createElement('div');
  badge.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:2147483647;font:12px/1.4 system-ui,sans-serif;' +
    'background:#111;color:#eee;padding:6px 9px;border-radius:6px;opacity:.85;pointer-events:none;max-width:320px';
  document.documentElement.appendChild(badge);
  function paint() {
    badge.innerHTML = `<b>Draft bridge</b>: ${bridgeOk ? 'verbunden ✓' : 'nicht erreichbar ✗'} · ` +
      `${hasRoom() ? 'Draft-Room erkannt ✓' : 'kein Draft-Room auf dieser Seite'}` +
      `<br>${sentCount} gesendet${failed ? `, ${failed} Fehler` : ''}${status ? '<br>' + status : ''}`;
  }
  function health() {
    GM_xmlhttpRequest({
      method: 'GET', url: CONFIG.BRIDGE + '/health',
      onload: (r) => { bridgeOk = r.status === 200; paint(); },
      onerror: () => { bridgeOk = false; paint(); },
    });
  }

  // ── run ──────────────────────────────────────────────────────────────────────────────────────
  let timer = null;
  new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(scan, 150); })
    .observe(document.body, { childList: true, subtree: true, characterData: true });
  setInterval(scan, CONFIG.SCAN_MS);
  setInterval(health, 5000);
  health(); paint();
})();
