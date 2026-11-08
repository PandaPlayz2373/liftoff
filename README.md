<p align="center"><img src="assets/logo.png" width="160" alt="Liftoff logo"></p>

<h1 align="center">Liftoff</h1>

<p align="center">Move a local Claude Code session to the cloud, keep working on it from any device, then bring the work back.</p>

---

You're deep in a Claude Code session on your laptop, and you need to leave. Say **"liftoff"** or **"move this to
the cloud"**. Liftoff packs up the project, your uncommitted work, what the conversation knows, the connections it
uses and the API keys it needs. It starts a [cloud session](https://code.claude.com/docs/en/claude-code-on-the-web)
that picks up exactly where you stopped. Open it on your phone (Claude app → **Code**), in a browser at
claude.ai/code, or on another computer. It keeps running with the laptop closed. Back at your desk, say
**"bring it back"** and the cloud's commits land in your local checkout.

## Install

In Claude Code:

```
/plugin marketplace add PandaPlayz2373/liftoff
/plugin install liftoff@liftoff
```

You need:
- Claude Code signed in with a claude.ai account on a plan that includes cloud sessions (Pro, Max, Team or
  Enterprise). An API key won't work.
- `git`, and the [GitHub CLI](https://cli.github.com) signed in (`gh auth login`). Cloud sessions clone from GitHub.
- Python 3.8+. On Windows, Liftoff installs `pywinpty` (into its own folder) the first time it launches a session.

## Use

Just ask, in any session:

| You say | What happens |
|---|---|
| "liftoff" / "move this to the cloud" | the full handoff below, ending with a link |
| "what's the cloud session doing?" | status, what it last said, the branch it pushed |
| "tell the cloud session to run the tests" | queues a message into it |
| "bring it back" / "pull the cloud changes" | merges the cloud session's branch into your local checkout |

## What it carries over

A cloud session only knows what's in the repo and its cloud environment. Liftoff makes sure that's everything:

1. **The code.** It creates a private GitHub repo if there isn't one, then commits and pushes your uncommitted work.
   It scans for secrets before every push, and secrets never go into git.
2. **The conversation.** Claude writes `.claude/cloud-handoff.md` from the session: the goal, decisions, current
   state, next steps, gotchas, and what's only available on your machine. The cloud session reads it first.
3. **Connections.** claude.ai connectors follow your account automatically. Local MCP servers that the work needs
   get added to the project's `.mcp.json`, and user-level skills and agents get copied into the project.
4. **Setup.** Dependencies go into a SessionStart hook in the repo, so every cloud session installs them.
5. **Keys, without exposing them.** Liftoff creates a cloud environment for the project and adds each API key as
   a [network secret](https://code.claude.com/docs/en/cloud-environments#add-network-secrets). Anthropic's proxy
   attaches the key to requests for that host after they leave the VM, so the cloud session can call the API but
   never sees the key. Keys are read from your local files and are never printed or put on a command line.
6. **Launch and check.** It starts the session, pins the environment for the project, and checks the session can
   actually do the work, including one real API call, before it gives you the link.

## The helper script

The skill drives `skills/liftoff/scripts/liftoff.py`, which you can also run yourself:

```
python liftoff.py check                                   # sign-in, cloud access, git, gh
python liftoff.py envs                                    # your cloud environments
python liftoff.py env-create <name> [--host h] [--var K=V] [--setup-file f]
python liftoff.py secret-add <env> api.example.com --value-file key.txt         # Authorization: Bearer <key>
python liftoff.py secret-add <env> api.example.com --header X-Api-Key --prefix none --value-env MY_KEY
python liftoff.py secret-add <env> api.example.com --basic-user me --password-stdin
python liftoff.py secret-list <env>                       # hosts and headers, never values
python liftoff.py pin <project> <env_id>                  # use that environment for this project
python liftoff.py launch <project> <prompt.md>            # start a cloud session, prints its link
python liftoff.py status <session>                        # state, environment, branch
python liftoff.py log <session>                           # what it last said and printed
python liftoff.py send <session> "<message>"              # follow-up message
python liftoff.py pull <session> [<project>]              # merge the session's branch locally
```

It's standard-library Python, and it uses Claude Code's own sign-in. `claude --cloud` refuses to run without a
terminal, so `launch` gives it a pseudo-terminal. It answers the folder trust prompt only for the project you
asked to send.

**A heads-up:** creating environments and secrets uses the same endpoints as the claude.ai web app. They work
with Claude Code's sign-in, but they aren't a documented public API, so Anthropic could change them. If they
ever fail, Liftoff falls back to telling you exactly what to paste into the claude.ai environment settings.

## Limits

- Cloud sessions use your plan's normal usage limits.
- A VM that sits idle gets reclaimed. Reopening the session restores the conversation, but not background
  processes.
- Anything that only exists on your machine stays there: local databases, hardware, other computers on your
  network, your browser. The handoff note says what's missing and what to do instead.
- Environments can't be archived through the API yet. Do that in the claude.ai environment settings.
- Secrets added through the API take exact hosts only. A `*.example.com` secret has to be added in the web UI.
- To continue the *conversation* locally (not just the code), run `claude --teleport <session-id>` in the project.

## License

MIT
