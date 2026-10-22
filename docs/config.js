// Backend base URL. The app runs fully client-side (GitHub Pages + Supabase + the scheduler Worker), so there is NO backend by default and CLAIRVOYANCE_API stays undefined -> offline mode.
// It used to auto-point at http://localhost:8000 when the page was opened from localhost; nothing listens there, so every local run (tests included) made dead requests (owner: removed 2026-10-22).
// To force a backend, set window.CLAIRVOYANCE_API = 'https://your-backend.example.com' here.
