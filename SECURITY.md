# Security

Please don't report security problems in public issues. Use GitHub's private vulnerability reporting
("Report a vulnerability" under the repository's Security tab) instead. You'll get an answer within a week.

Things worth knowing when you run SEO Desk:

- Your keys stay in `.env` in your SEO Desk folder. Keep it out of version control.
- The dashboard listens on 127.0.0.1. Before you expose it, set a password (`seodesk password`) and put HTTPS in front
  of it. `--no-password` refuses any request that didn't come from the machine itself or that came through a proxy.
- The dashboard password is stored as a PBKDF2-SHA256 hash, sessions are signed HttpOnly cookies, failed sign-ins are
  rate limited, and every change made through the dashboard needs a header other sites can't send (CSRF guard).
