# Security Policy

Simo Flow is a privacy-first application: its entire purpose is to keep your
voice and its transcripts on your own machine. Security is therefore a feature,
not an afterthought. This document describes the threat model, the protections
in place, and how to report a vulnerability.

## Threat model

**What Simo Flow protects**

- **Your audio.** Speech is transcribed on-device by `whisper.cpp` (Metal) and
  cleaned by a local Ollama model. Audio is never written to disk and never sent
  off the machine.
- **Your transcripts.** History lives only in a local SQLite database at
  `~/.simo-flow.db`, created with `0600` permissions (owner-read/write only).
  The log at `~/.simo-flow.log` is likewise `0600`. Neither is ever transmitted.
- **The local dashboard.** The FastAPI server binds `127.0.0.1` only.

**What is explicitly out of scope**

- Physical access to an unlocked machine. Local files are protected by Unix
  permissions, not encryption at rest.
- The security of the upstream models/tools (`whisper.cpp`, Ollama) themselves.

## Protections in place

| Risk | Mitigation |
|------|-----------|
| Any network egress of your data | No outbound network calls exist. STT is local; the only HTTP is to `127.0.0.1` (whisper-server, Ollama). |
| A website reading your history (DNS-rebinding) | `Host` header is validated on **every** dashboard route; non-localhost hosts are rejected `403`. |
| A website writing to your dictionary (CSRF) | `Origin` header is checked on state-changing routes. |
| SQL injection via search / dictionary input | All queries are parameterized; no string-built SQL. |
| XSS in the dashboard | All user-controlled values are escaped (`textContent` → `innerHTML`) before rendering. |
| World-readable transcripts on a shared machine | DB and log are `0600`, tightened on upgrade as well as creation. |
| Dictated text lingering on the clipboard | Clipboard is snapshot and restored around paste; non-restorable content is cleared, not left. |

The dashboard has **no authentication** by design — it is reachable only from
`localhost` on your own machine. That is a deliberate choice for a single-user
local tool, not an oversight; the `Host`/`Origin` checks above are what stop a
browser you're using from being turned against it.

## Verify it yourself

Don't take "your audio never leaves your machine" on trust — confirm it. The
dictation path makes zero outbound network calls; the only sockets are to
`127.0.0.1` (whisper-server on `:7332`, Ollama on `:11434`, the dashboard on
`:7331`). You can watch for yourself:

```bash
# nothing should appear for simo-flow beyond 127.0.0.1 connections
nettop -p "$(pgrep -f 'python -m engine' | head -1)"
```

Or point a network monitor (Little Snitch, a proxy) at it and dictate — you'll
see no egress. The app was built by tearing down a competitor that quietly sent
every word to its servers, so this app invites exactly the scrutiny that one
failed.

## Supported versions

The latest release on `main` is supported. This is a single-maintainer project;
security fixes land on `main` and are noted in [CHANGELOG.md](CHANGELOG.md).

## Reporting a vulnerability

If you find a security issue, please **do not open a public issue.** Instead:

- Use GitHub's **private vulnerability reporting** ("Report a vulnerability" on
  the Security tab of the repository), or
- Contact the maintainer directly via the email on the GitHub profile.

Please include reproduction steps and the impact you observed. You'll get an
acknowledgement as soon as possible, and credit in the changelog if you'd like
it once a fix ships.
