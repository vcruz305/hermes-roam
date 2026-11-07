---
name: roam-setup
description: Use when the user wants to sync their Hermes Agent home across two or more machines with hermes-roam. Covers generating the archive key, choosing a backend (Cloudflare R2, S3, private git repo, or a local directory), running hermes roam init/doctor/push, and pulling on the second device.
---

# Hermes Roam setup

Roam syncs a Hermes home between the user's own machines as one encrypted
bundle. One device pushes a generation; the other pulls it. Walk the user
through the steps below. Never read `.env`, `auth.json`, or any other
credential file yourself — never open, print, or summarize them — and never
ask the user to paste a secret into the chat: secrets go into `{home}/.env`
(the active Hermes home's `.env`) by the user's own hand.

**Who runs what.** The destructive and fleet-creating commands are the
USER's, always: the user runs `hermes roam init --create-remote`,
`hermes roam push`, and `hermes roam pull --apply` themselves, on their own
machine, after reviewing what they do. Never run those three yourself and
never run them through the chat tools. Read-only checks you may run for the
user are `hermes roam status`, `hermes roam diff`, and `hermes roam doctor`
(or the `/roam status` / `/roam diff` slash forms).

## What syncs and what stays local

In plain words:

- **Always synced (shared):** `config.yaml` (minus host-specific keys),
  `SOUL.md`, `memories/`, `skills/`, `skins/`, `hermes-pack.yaml`, and the
  same set inside named profiles. This list is an allowlist: anything not on
  it stays on its machine. Plugin code is not synced (roam records installed
  plugin names and pins so the other device can reinstall them), and skills
  installed from the hub are not synced either — reinstall them from the hub.
- **Never synced (local):** `.env`, `auth.json`, lock files, `state.db`,
  delivery and execution databases, the gateway, `bin/`, `hermes-agent/`,
  `plugin-data/`, per-profile homes, `hooks/` (Hermes runs hooks at startup,
  so syncing them would run remote code on the other device), and anything
  else the allowlist does not name.
- **Opt-in, off by default:** the read-only session archive
  (`sync_sessions`: publishes this device's transcripts, exported by
  Hermes's own CLI, capped, read-only everywhere it lands — other devices
  list them with `hermes roam sessions` but can never resume or re-push
  them; the raw `sessions/` request-dump folder never travels) and cron
  job definitions only (`sync_cron`; run output never travels).
- **Ignored entirely:** caches, logs, backups, sandboxes, temp dirs, and the
  hub's own state under `skills/` (lockfiles, locks, archives).
- **Host-scoped config keys** (terminal, paths, display interface, browser
  executable, MCP server commands) are stripped on push and kept on pull.
- **Secrets are never synced.** No bundle ever contains `.env`, `auth.json`,
  or any credential value.

## Step 1 — generate the archive key

Ask the user to run this on the first machine and keep the output private:

```
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

That is 32 random bytes in base64url — exactly what roam's key decoder
accepts. The user must put it in `{home}/.env` as
`ROAM_ARCHIVE_KEY=...` on **every** device in the fleet. The same key
encrypts every bundle and signs the remote head; a different key means the
other device's data will not verify.

## Step 2 — choose a backend

- **Cloudflare R2** — recommended: free tier covers a typical home, no
  egress charges.
- **AWS S3** or any S3-compatible store.
- **Private git repo** — works over SSH with the user's own keys.
- **Local directory** — tests and demos only; both "devices" must reach the
  same disk.

## Step 3 — Cloudflare R2 (human steps for the user)

1. Create a free Cloudflare account at https://dash.cloudflare.com (no paid
   plan needed).
2. In the R2 section, accept the one-time enable prompt. Until the user does,
   the storage endpoint serves no TLS certificate and every connection fails.
3. Create an R2 bucket, e.g. `hermes-roam`. Do not enable public access; the
   bucket stays private.
4. In R2, create an API token scoped to **Object Read & Write** on that one
   bucket only (not an account-wide key, not Workers R2 Storage Edit).
5. The user puts the token's Access Key ID and Secret Access Key in
   `{home}/.env` as `ROAM_S3_ACCESS_KEY_ID=...` and
   `ROAM_S3_SECRET_ACCESS_KEY=...`.
6. Settings for roam: backend `s3`, bucket name as above, endpoint
   `https://<account_id>.r2.cloudflarestorage.com` (the account id is on the
   Cloudflare dashboard's right sidebar), region `auto` (the literal string).

For AWS S3 the user picks a region and an IAM key limited to that bucket, and
leaves the endpoint empty.

## Step 3 (git backend) — private repo and SSH keys

1. The user creates a **private** git repository (GitHub, GitLab, or their own
   server) and adds their SSH public key on **both** machines.
2. Settings for roam: backend `git`, the SSH remote URL (`git@...`), branch
   `roam` (the default). Roam never stores git credentials; it uses the
   user's existing SSH setup with a per-home mirror clone.

## Step 4 — configure roam settings

The user sets the backend fields under
`plugins.entries.hermes-roam.settings` in `config.yaml` (`hermes config set`
or the Desktop plugins page): `backend`, `s3_bucket`/`s3_endpoint_url`/
`s3_region`, or `git_remote`, or `local_dir`, and a `device_name` per machine
(e.g. `desktop`, `laptop`).

## Step 5 — initialize, check, push

Have the USER run these on the first machine — you never run
`init --create-remote`, `push`, or `pull --apply` yourself:

```
hermes roam init --create-remote
hermes roam doctor
hermes roam push
```

- `init --create-remote` validates settings, pings the backend, and writes the
  generation-0 head and the local state.
- `doctor` must pass every check before the first push (key decodes, backend
  reachable, head signature verifies, directories writable); you may run
  `hermes roam doctor` yourself to check the result.
- `push` builds and uploads generation 1.

## Step 6 — second device

1. The user installs the same plugin and puts the **same**
   `ROAM_ARCHIVE_KEY` (plus the backend secrets, if S3/R2) in that
   machine's `{home}/.env`, and the same backend settings in `config.yaml`.
2. Have the USER run:

```
hermes roam pull
```

   and review the plan it prints together with them. Nothing is written yet.
3. When the plan looks right, have the USER run:

```
hermes roam pull --apply
```

   You never run `pull --apply` yourself. This rewrites the home's shared
   files after copying the replaced files to a backup directory; on failure
   the install rolls back. Host-specific config keys stay local. Afterward,
   reinstall the plugins roam lists with `hermes plugins install <name>`.

## Ongoing use

- `hermes roam status` — fleet and generation status on any device.
- The user pushes after meaningful changes (`hermes roam push`) and pulls on
  the others (`hermes roam pull`, then `hermes roam pull --apply`) — those
  two commands stay in the user's hands on every machine.
- Pushes refuse when another device moved ahead (exit code 2): pull first.
- `/roam status` and `/roam diff` work from chat; `/roam pull apply` is the
  only form that writes, and it is the user's to send.
