const Tracker = (() => {
  async function loadData() {
    const res = await fetch('data.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('data.json not found');
    return res.json();
  }

  const fmtInr = n => n == null ? '—' :
    '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const fmtPct = n => n == null ? '—' : (n > 0 ? '+' : '') + Number(n).toFixed(2) + '%';
  const pnlClass = n => n == null ? '' : (n >= 0 ? 'pos' : 'neg');
  const pairId = (commodity, stock) => `${commodity}__${stock}`;

  // generated_at is written by Python's datetime.now() on the GitHub
  // Actions runner, which runs in UTC, as a naive (no offset) ISO string.
  // Display it in IST with an explicit label so it's not mistaken for
  // local/IST time as-is.
  function fmtIST(isoNaiveUtc) {
    if (!isoNaiveUtc) return '—';
    try {
      const d = new Date(isoNaiveUtc.endsWith('Z') ? isoNaiveUtc : isoNaiveUtc + 'Z');
      if (isNaN(d)) return isoNaiveUtc;
      return d.toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', year: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true,
      }) + ' IST';
    } catch (e) {
      return isoNaiveUtc; // never let a formatting hiccup blank the whole footer
    }
  }

  // Google search link so the price/chart page for a stock is one click away.
  // "<TICKER> stock price" reliably surfaces Google's own price/chart card.
  function googleSearchLink(query) {
    return `https://www.google.com/search?q=${encodeURIComponent(query)}`;
  }
  function stockSearchLink(stock) {
    const bare = stock.replace('.NS', '');
    return googleSearchLink(`${bare} stock price NSE`);
  }
  function commoditySearchLink(commodity) {
    const label = { gold: 'gold price today', silver: 'silver price today', copper: 'copper price today' }[commodity] || commodity;
    return googleSearchLink(label);
  }

  function renderNav(active) {
    const tabs = [
      ['index.html', 'Tracking'],
      ['active.html', 'Active Trades'],
      ['history.html', 'History'],
    ];
    return `<nav class="tabs">${tabs.map(([href, label]) =>
      `<a href="${href}" class="${active === href ? 'active' : ''}">${label}</a>`).join('')}</nav>`;
  }

  function qs(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  return { loadData, fmtInr, fmtPct, pnlClass, pairId, fmtIST, stockSearchLink, commoditySearchLink, renderNav, qs };
})();
