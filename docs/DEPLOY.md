# Deploying to Railway

What you get at the end: Instagram and Telegram both answering patients, and
the operator dashboard reachable over HTTPS — one deployment, one clinic, one
knowledge base shared by both platforms.

The whole setup is driven by environment variables. That is not a style
choice: on Railway the database is reachable only from inside the project's
private network, so no script you run from your own machine can write the
first rows. The application provisions itself on startup instead — see
`app/core/provisioning.py`.

---

## What Railway will be running

Four services in one Railway project:

| Service    | What it is                     | Source                          |
| ---------- | ------------------------------ | ------------------------------- |
| `web`      | FastAPI — webhooks + dashboard | this repo, repository root      |
| `worker`   | arq — answers and reminders    | this repo, repository root      |
| `postgres` | Postgres 16 **with pgvector**  | image `pgvector/pgvector:pg16`  |
| `redis`    | Redis 7                        | image `redis:7-alpine`          |

`web` and `worker` build from the **same** Dockerfile and run the same code.
The only difference between them is `APP_ROLE` — see the `CMD` at the bottom
of the Dockerfile.

> **Do not use Railway's stock Postgres.** The knowledge base is stored as
> vectors and the first migration runs `CREATE EXTENSION vector`. Railway's
> default Postgres image does not have pgvector, and the migration will fail.
> Deploy `pgvector/pgvector:pg16` as a Docker image service instead.

---

## Step 1 — Generate the three secrets

Run this once, on your own machine, from the repository root:

```powershell
../.venv/Scripts/python.exe scripts/generate_secrets.py
```

It writes `.env.railway` — a ready-to-paste block of every variable Railway
needs, with the secrets already generated and the rest left blank for you to
fill in. That file is gitignored and must stay that way: it holds live
credentials.

The three generated values are worth understanding, because losing them costs
different things:

- **`ENCRYPTION_KEY`** — encrypts the Instagram and Telegram tokens stored in
  the database. Lose it and every channel credential becomes unreadable and
  has to be re-entered. Do not reuse your local development key here.
- **`SESSION_SECRET_KEY`** — signs the dashboard login cookie. Changing it
  logs everybody out. That is all it costs.
- **`WEBHOOK_VERIFY_TOKEN`** — a string you and Meta both know, used once
  when Meta verifies the webhook URL. You will paste it into the Meta app
  console later.

## Step 2 — Create the project and the two data services

1. Railway → **New Project** → **Empty Project**.
2. **+ New** → **Database** → **Add Redis**.
3. **+ New** → **Docker Image** → `pgvector/pgvector:pg16`. Then on that
   service:
   - **Variables**: `POSTGRES_USER=postgres`,
     `POSTGRES_PASSWORD=<something long>`, `POSTGRES_DB=dental`
   - **Settings → Volumes**: add a volume mounted at
     `/var/lib/postgresql/data`. Without it, every redeploy starts with an
     empty database.

Name it `postgres`. The name becomes its private hostname
(`postgres.railway.internal`), which the next step refers to.

## Step 3 — Create the `web` service

1. **+ New** → **GitHub Repo** → this repo. Branch: `main` for a new doctor;
   the first clinic's project tracks `stable`, which is promoted from `main`
   after a day or two in use.
2. **Settings → Root Directory**: leave it empty (the repository root)
3. **Settings → Networking → Generate Domain**. Copy the result — something
   like `https://web-production-a1b2.up.railway.app`. This is your
   `PUBLIC_BASE_URL`, and every webhook URL below is built from it.
4. **Settings → Deploy → Pre-deploy Command**:
   ```
   alembic upgrade head
   ```
   On the `web` service only. Railway runs it once per deploy, before the new
   version takes traffic. Putting it on the worker too would race a second
   copy of itself against the first.
5. **Settings → Deploy → Healthcheck Path**: `/health`
6. **Variables → Raw Editor**: paste the contents of `.env.railway`.

Two variables you fill in by hand there:

```
DATABASE_URL=postgresql+asyncpg://postgres:<the password>@postgres.railway.internal:5432/dental
REDIS_URL=${{Redis.REDIS_URL}}
PUBLIC_BASE_URL=https://<the domain from step 3.3>
```

`${{Redis.REDIS_URL}}` is Railway's own reference syntax — type it literally
and Railway substitutes the running Redis URL.

## Step 4 — Create the `worker` service

Same repo, same branch, same (empty) root directory. Then:

- **Variables**: the same block as `web`, plus **`APP_ROLE=worker`**.
- **No** pre-deploy command, **no** healthcheck, **no** domain. It has no
  HTTP port; a healthcheck would fail it forever.

The provisioning variables (`PROVISION_*`) are read on web startup only, so
they are harmless on the worker — but the worker does need `DATABASE_URL`,
`REDIS_URL`, `ENCRYPTION_KEY` and `OPENAI_API_KEY`, or it cannot answer
anything.

> Keep `ENCRYPTION_KEY` **identical** on both services. The worker decrypts
> the token the web process encrypted; two different keys means every reply
> is generated and then silently dropped.

## Step 5 — First deploy

Deploy `web`. Watch its logs for these lines:

```
provisioned_telegram_channel  tenant_id=… bot_id=… username=…
provisioned_telegram_webhook  url=https://…/webhook/telegram/…
provisioned_operator          tenant_id=… username=… role=operator
```

Every provisioning step is deliberately logged at WARNING, so they are easy
to find without widening a log filter. If one is missing, the same step logs
why:

| Log line                                          | What it means                                       |
| ------------------------------------------------- | --------------------------------------------------- |
| `provisioning_skipped_telegram reason=public_base_url_not_https` | `PUBLIC_BASE_URL` is missing or not `https://` |
| `provisioning_failed step=telegram`               | the bot token was rejected — check @BotFather        |
| `provisioning_failed step=operator:…`             | usually the password is under 10 characters          |
| `provisioning_noop_operator_exists`               | already created; this is the normal state after boot |
| `provisioning_timed_out step=…`                   | the database was unreachable within 15s              |

None of them stop the app. A deployment that cannot provision still serves,
because the webhook traffic it would drop while refusing to start is not
recoverable and the provisioning problem is.

Then deploy `worker` and confirm its log says `redis_version=…` and
`Starting worker for 3 functions`.

## Step 6 — Log in and load the knowledge base

Open `https://<your domain>/admin` and log in with
`PROVISION_OPERATOR_USERNAME` / `PROVISION_OPERATOR_PASSWORD`.

**Change that password immediately** from the Sozlamalar tab. The bootstrap
password is sitting in Railway's variable editor in plain text, visible to
anyone with access to the project. Changing it in the dashboard is safe:
provisioning never resets an account that already exists, so the next deploy
will not put the old one back.

Then load the FAQs. Set `SEED_FAQS_FROM=<the doctor's FAQ file>` on `web` (see `data/faqs.example.json`), redeploy,
watch for `seeded_faqs tenant_id=… rows=…`, then **remove the variable
again** — left set, every redeploy re-embeds the whole file.

Until the knowledge base has rows in it, the assistant answers from the clinic
details in the settings and its own general knowledge.

## Step 7 — Telegram

Nothing left to do. Step 5 already called `setWebhook` for you. Message the
bot and watch the `worker` logs.

If you want to check what Telegram thinks the webhook is:

```
https://api.telegram.org/bot<TOKEN>/getWebhookInfo
```

`pending_update_count` climbing while nothing is answered means the endpoint
is rejecting deliveries — almost always a `PUBLIC_BASE_URL` that does not
match the domain Railway actually serves.

## Step 8 — Instagram

Instagram needs Meta's approval, so it comes last and it is the one step that
is not under your control.

In the Meta app console, Webhooks → Instagram:

- **Callback URL**: `https://<your domain>/webhook/instagram`
- **Verify Token**: your `WEBHOOK_VERIFY_TOKEN`
- **Subscribe to**: `messages`
- **Privacy Policy URL**: `https://<your domain>/privacy` — Meta will not
  publish an app without one, and an unpublished app is exactly why a
  deployment receives no Instagram webhooks at all.

Then set on `web`:

```
PROVISION_IG_ACCOUNT_ID=<the 17841… Instagram Business Account id>
ACCESS_TOKEN=<the long-lived page access token>
```

`PROVISION_IG_ACCOUNT_ID` is the id Meta sends as `entry.id` in the webhook —
**not** the id `graph.instagram.com/me` returns. They are different
namespaces, and getting it wrong shows up as `webhook_unknown_ig_account` on
every delivery while provisioning reports success.

Redeploy. A channel already seeded without a token has it filled in
(`provisioned_channel_token_filled`); a channel whose credential you set by
hand is never overwritten.

---

## Costs and what to watch

Railway bills by usage. Four services of this size land around $10–20/month.
Replies and embeddings both go to OpenAI, billed per token on the key in
`OPENAI_API_KEY`. Changing the embedding model later means re-embedding the
entire knowledge base, because vectors from two models are not comparable —
`knowledge_base.embedding_model` records which model made each row, and
`ingest_faqs` re-embeds any row that disagrees with the configured one, so a
change repairs itself on the next seed rather than silently emptying
retrieval.

Back the database up. Patient appointments and the knowledge base are the two
things in this system that cannot be rebuilt from the repository.

## Rolling a secret

- **Bot token** — regenerate in @BotFather, update
  `PROVISION_TELEGRAM_BOT_TOKEN`, redeploy. Provisioning replaces the stored
  token and re-registers the webhook.
- **`SESSION_SECRET_KEY`** — change it, redeploy. Everyone is logged out.
- **`ENCRYPTION_KEY`** — this one is not a simple swap. Every stored channel
  credential was encrypted with the old key and becomes unreadable; the app
  treats an unreadable credential as "not configured" and stops sending
  rather than failing. Plan to re-provision both channels in the same deploy.
