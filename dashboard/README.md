# Chomage dashboard

Web dashboard for the Chomage bot: read views over the bot's Postgres
(seasons, weekly awards, tracked players, bot config) plus runtime
controls. Login is **Discord OAuth2 only**, restricted to members of the
Discord server; config writes are further restricted to an admin
allowlist.

Runs in its own container (planned: a fresh LXC on the Proxmox host),
talking to Postgres in CT 105. It never touches the bot process — the
control surface is the `bot_config` table, which the bot already polls
at runtime.

## One-time setup

### 1. Discord OAuth2 credentials

1. https://discord.com/developers/applications → select the existing bot
   application (or create one).
2. **OAuth2** tab → **Redirects** → add your callback URL, e.g.
   `http://192.168.0.X:8000/auth/callback`. It must byte-match
   `oauth_redirect_uri` in `.env` (scheme, host, port, path).
3. Copy the **Client ID** and **Client Secret** into `.env`.

No bot token is needed — the app checks guild membership with the
`guilds` OAuth scope of the logged-in user.

### 2. Database role

Give the dashboard its own role: read everything, write only bot_config.

```sql
CREATE ROLE chomage_dash LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE chomage TO chomage_dash;
GRANT USAGE ON SCHEMA public TO chomage_dash;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO chomage_dash;
ALTER DEFAULT PRIVILEGES FOR ROLE chomage IN SCHEMA public
    GRANT SELECT ON TABLES TO chomage_dash;
GRANT INSERT, UPDATE ON bot_config TO chomage_dash;
```

### 3. Configure and run

```sh
cp .env.example .env   # fill in everything
docker compose up -d --build
```

Visit `http://<host>:8000`, log in with Discord.

## How the login works

Authorization-code flow: `/login` redirects to Discord with our client
id + a signed `state`; the user approves `identify guilds`; Discord
redirects back to `/auth/callback` with a one-time code; the server
exchanges code + client secret for an access token (secret stays
server-side), asks Discord who the user is and which guilds they're in,
and only guild members get a signed session cookie (7 days). No
passwords, and the Discord access token is discarded after those two
lookups.

## Controls

`POST /config` (admins only) upserts a `bot_config` key. The bot polls
this table, so changes take effect without touching the bot — e.g.
`ranked5s_channel_id`. New runtime switches should follow the same
pattern: add a bot_config key the bot reads, control it from here.
