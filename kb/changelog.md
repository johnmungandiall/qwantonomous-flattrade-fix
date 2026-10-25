# Changelog — notable changes, newest first.

The ONLY place release history lives (don't duplicate it in other notes).
One line per change that future-you or the agent should know about.

## Unreleased
- **v1.0.01 zip downloads had no UI and crashed.** `frontend/dist` was git-ignored, so a source archive of any branch whose CI never ran `commit-dist` shipped no build; with `react_bp` unregistered, `serve_react_app`'s bare `(html, 503)` tuple was re-paired by the 404 handler into `((html,503),404)`, raising a TypeError **from inside an error handler** and looping `/error` → 500 → `/error` (ERR_TOO_MANY_REDIRECTS). Fixed by committing `frontend/dist` (`.gitignore` now negates it, `.gitattributes` keeps the bytes verbatim), returning a real `Response` from `blueprints/react_app.py#serve_react_app`, making `app.py#internal_server_error` redirect only when a build exists and it is not already on `/error`, and answering `serve_react_app()` from the `blueprints/auth.py#login` GET fallbacks and `app.py#method_not_allowed_error`. The 31 TypeScript errors that blocked `npm run build` (retired `'strategy'` toast category, unused `event` param) are fixed too. `.env` reads now state `utf-8`. See [[gotchas]].

## (date) - first version
- Initial knowledge base scaffolded.
