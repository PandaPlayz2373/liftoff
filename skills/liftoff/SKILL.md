---
name: liftoff
description: Move the current Claude Code session to the cloud so it keeps working on any device, then bring the work back. Use when the user says "liftoff", "move this to the cloud", "make this a cloud session", "send this chat to the cloud", "I want to keep working on this from my phone", "keep this going while my laptop is closed", or later "bring it back", "pull the cloud changes", "what did the cloud session do". Carries the project folder, uncommitted work, what this chat knows, connections, keys and setup, then starts a cloud session that picks up where this one left off.
---

# Liftoff

A local Claude Code session lives on one machine. A **cloud session** runs on Anthropic's infrastructure, keeps
going with the laptop closed, and opens from the browser (claude.ai/code), the Claude mobile app (**Code** tab),
the Desktop app or another terminal. This skill moves a local session into one, then gets the results back.

The cloud only gets what you carry over. It clones the repo from GitHub. It doesn't see this conversation, the
user's machine, their home-folder config, local-only MCP servers or local secrets. So the work here is packing
everything the next session needs into the repo and the cloud environment, then checking that it actually works
over there.

Helper (stdlib Python, uses the Claude Code CLI's own sign-in):
`python3 "${CLAUDE_PLUGIN_ROOT}/skills/liftoff/scripts/liftoff.py" <command>`. On Windows use `python`.
If `CLAUDE_PLUGIN_ROOT` isn't set, the script sits next to this file in `scripts/`.

| command | does |
|---|---|
| `check` | Claude sign-in, cloud access, git, gh |
| `envs` | lists cloud environments (`env_... name`) |
| `env-create <name> [--host h]... [--var K=V]... [--setup-file f]` | new environment, prints `ENV <id>` |
| `secret-add <env> <host> [--header H] [--prefix P] --value-file F` | network secret (also `--value-env`, `--value-stdin`, or `--basic-user U --password-file F`) |
| `secret-list <env>` / `secret-delete <env> <id>` | hosts and headers only, never values |
| `pin <project> <env_id>` | makes cloud sessions from this project use that environment |
| `launch <project> <prompt.md>` | starts the cloud session, prints `SESSION <url>` |
| `status <session>` / `log <session>` | state and branch / what it last said and printed |
| `send <session> "<msg>"` | queues a follow-up into the running session |
| `pull <session> [project]` | merges the cloud session's branch into the local checkout |

Do every step yourself, including environments and secrets. Only stop for things that need the user's hands,
like a GitHub or claude.ai sign-in. Then give them the link.

## Send: local session -> cloud

### 1. Pick the project and check the basics
The project is the folder this session works in. Never send a whole home directory. If you can't tell which
folder, ask once. Run `check` and fix anything it flags first. Cloud sessions need a claude.ai sign-in (not an API
key) and a GitHub-hosted repo.

### 2. Get everything into GitHub
- **Not a git repo**: `git init`, then write a `.gitignore` that keeps out secrets (`.env*`, credential and token
  files, keys), dependency folders, build output, logs and large media. Create a **private** repo with
  `gh repo create <name> --private --source . --push`.
- **A repo already**: commit the uncommitted work on the current branch ("WIP: continue in cloud session") and
  push it. The cloud clones the pushed branch, not the working tree.
- **Scan before every push.** Grep what's staged for passwords, API keys, tokens and private keys. A secret never
  goes into git. It becomes a network secret in step 4.
- If the remote isn't GitHub, stop and say so (cloud sessions clone from GitHub).

### 3. Write the handoff from this conversation
Write `.claude/cloud-handoff.md`, replacing any old one. Write it for a capable engineer with zero context:
- **Goal**: what the project is and what the user is trying to get done right now
- **Decisions and preferences**: what was settled here, and how the user likes to work
- **State**: what works, what's half done, and the next steps in order
- **Gotchas**: things learned the hard way, commands that matter, how to test
- **Not in the cloud**: local paths, local tools, other machines, the user's browser, local-only services.
  For each one, say what to do instead.
- **How the user will talk to it**: often from a phone, maybe by voice, so short replies and act on clear requests.

If the project has no `CLAUDE.md`, create a short one that points at the handoff. If the user's personal
`~/.claude/CLAUDE.md` holds preferences that matter for this work, copy the relevant lines into the handoff. That
file doesn't exist in the cloud.

### 4. Carry the connections and setup
Go through what this session relies on and bring each piece across:
- **MCP servers**: claude.ai connectors follow the account into cloud sessions by themselves. Servers configured
  only in the user's local config don't. For each one this work needs:
  - remote (HTTP/SSE) server with no secret in it: add it to the project's `.mcp.json`
  - local (stdio) server that installs from a registry (`npx ...`, `uvx ...`): add it to `.mcp.json`
  - anything needing a key: handle it like any other secret (below)
- **Skills and agents**: user-level skills or agents this work uses don't exist in the cloud. Copy the ones it
  needs into the project's `.claude/skills/` or `.claude/agents/`, after checking they contain nothing private.
- **Dependencies**: if setup takes more than the base image (which has common languages and package managers),
  add a SessionStart hook to the project's `.claude/settings.json` that installs it. A hook is versioned with
  the repo and runs in every cloud session:
  `{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "<install command>"}]}]}}`
- **Network, keys and settings**: list the hosts the work calls beyond GitHub and package registries, the keys
  it uses, and where each key lives locally (a `.env`, a creds file, an env var). Run `envs` and `secret-list`
  on any likely match.
  - **Nothing extra**: pin the plain default environment.
  - **Otherwise create a dedicated environment** for the project: `env-create <project-name>` with `--host` for
    each host it calls without a key, and `--var KEY=value` for non-secret settings. Never put a secret in
    `--var`, because anyone using the environment can read variables. Print nothing secret.
  - **For each key**, add a network secret for its host. Anthropic's proxy adds the header after the request
    leaves the VM, so the session can use the key without ever seeing it:
    `secret-add <env> api.example.com --header Authorization --prefix Bearer --value-file <local file holding just the key>`.
    For a key inside a `.env` or JSON file, write just that value to a temp file in the scratchpad, pass it with
    `--value-file`, then delete the temp file. Hosts must be exact (`api.example.com`): add one secret per host. A service that
    hops across many subdomains needs a `*.domain` secret, which only the web UI accepts (fallback below). For HTTP Basic auth use
    `--basic-user <user> --password-file <f>`. For a custom header (`X-Api-Key`), pass `--header X-Api-Key --prefix none`.
  - **Make the code work without the key**: in the cloud the local key file is missing. The code must then send
    no auth header of its own, or it will override what the proxy adds. Patch it if needed. A small
    "if the key file exists, add the header" change is usual.
  - The secret only reaches the hosts listed. A secret for a host that's also in an existing environment's
    secrets is fine. Each environment holds its own.
  - **If `env-create` or `secret-add` fails** (these use the same endpoints as the claude.ai web app, which
    aren't a documented public API), fall back to the web UI: claude.ai/code > environment selector > **New
    environment**, then the gear icon > **Network secrets** > **Add secret**. The fields are **Allowed
    websites**, **Header name**, **Prefix** and **Value**. Put the value on the user's clipboard, never print
    it, and give them the exact field values.
- **Always pin an environment** with `pin <project> <env_id>`, even when it's just the default one. Without a pin,
  the CLI falls back to whichever environment it finds first, which may carry another project's secrets. Then
  commit and push everything from this step.

### 5. Launch and verify
Write the first message to a temp file, along the lines of: "Read .claude/cloud-handoff.md and CLAUDE.md, then
continue from where the local session stopped: <next step>. Confirm in two lines what you're doing." Run
`launch`. About a minute later run `status` and `log`. If the project uses a key, make the first message include one
harmless call to that API so `log` shows it working. If anything looks wrong, `send` it a check ("run the tests",
"call the API once"). Fix any wall it hits (401, blocked host, missing tool) before you report back.

### 6. Report
Give the link and one line on where to find it: claude.ai/code, or the Claude app > **Code**, at the top of the
list. Say what stays local-only. Note the session id in the handoff file so a later local session can find it.

## Bring it back: cloud -> local

When the user is back at the machine and wants the cloud's work:
1. `status <session>` shows the branch it pushed.
2. `pull <session> <project>` merges it into the local checkout (refuses if there are local uncommitted changes).
   To continue the *conversation* locally instead, the user can run `claude --teleport <session-id>` in the project.
3. Summarise what changed from the commits, and update the handoff file if both sides will keep working.

## Notes
- Cloud sessions count toward the account's normal usage limits. The VM itself costs nothing extra.
- An idle cloud session's VM is reclaimed. Reopening it restores the conversation, but not background processes.
- `claude --cloud` needs a terminal, so `launch` runs it in a pseudo-terminal and answers the folder trust prompt
  for this project only.
