// Auto-detect backend: use localhost when running locally, offline mode on GitHub Pages
(function () {
  var local = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
  if (local) {
    window.CLAIRVOYANCE_API = 'http://localhost:8000';
  }
  // On GitHub Pages (hostname != localhost): CLAIRVOYANCE_API stays undefined → offline mode
  // To force a specific URL, set window.CLAIRVOYANCE_API = 'https://your-backend.railway.app' here
})();
