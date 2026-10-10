// Auto-detect backend: use localhost when running locally, offline mode on GitHub Pages
(function () {
  var local = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
  if (local) {
    window.CLAIRVOYANCE_API = 'http://localhost:8000';
  }
  // On GitHub Pages (hostname != localhost): CLAIRVOYANCE_API stays undefined → offline mode
  // To force a specific URL, set window.CLAIRVOYANCE_API = 'https://your-backend.railway.app' here
})();

// Cloudflare R2 mirror of the two high-churn files (docs/picks_backup.json, docs/live_data.json) -- see docs/R2_SETUP.md.
// EMPTY (the default) = the app reads both from this site exactly as before. Set it to the bucket's public base URL with NO trailing slash, e.g.
//   window.CV_R2_BASE = 'https://data.clairvoyanceengine.info';   (custom domain)    or    'https://pub-xxxxxxxx.r2.dev'
// and the app tries R2 first, falling back to the same-origin copy on any failure. (A value already set before this file loads -- e.g. by a test -- wins.)
window.CV_R2_BASE = window.CV_R2_BASE || '';
