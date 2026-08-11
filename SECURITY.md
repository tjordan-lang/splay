# Security

Please report security issues privately through GitHub's **Security** tab instead of opening a public issue.

Splay reads TIDAL developer credentials from `TIDAL_CLIENT_ID` and `TIDAL_CLIENT_SECRET`. Never commit those values. TIDAL OAuth tokens are stored locally at `~/.splay/tidal-session.json` with user-only permissions. Delete that file to remove the saved session.
