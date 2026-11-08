#!/usr/bin/env python3
"""Liftoff helper: move a local Claude Code session's project to a cloud session and back.

  liftoff.py check                         sign-in, git, GitHub CLI and cloud access all OK?
  liftoff.py envs                          your cloud environments (id, name)
  liftoff.py pin <project> <env_id>        make cloud sessions from this project use that environment
  liftoff.py launch <project> <prompt.md>  start a cloud session with that first message; prints SESSION <url>
  liftoff.py status <session>              a session's state, environment, repo and branch
  liftoff.py send <session> "<message>"    queue a follow-up message into a running cloud session
  liftoff.py pull <session> [<project>]    bring the cloud session's commits back into the local checkout
  liftoff.py log <session> [n]             the last n things the cloud session said or printed

  liftoff.py env-create <name> [--host h]... [--only-listed-hosts] [--var K=V]... [--setup-file f]
                                           new cloud environment; prints ENV <id>
  liftoff.py secret-add <env_id> <host> [--header H] [--prefix P] --value-file F|--value-env V|--value-stdin
  liftoff.py secret-add <env_id> <host> --basic-user U --password-file F|--password-env V|--password-stdin
                                           network secret: the proxy adds it to requests for <host>
                                           (exact host, no wildcards); the session never sees it
  liftoff.py secret-list <env_id>          secrets on an environment (hosts and header names, never values)
  liftoff.py secret-delete <env_id> <id>

Stdlib only, except `launch` on Windows, which needs pywinpty (installed on first use).
Uses the Claude Code CLI's own sign-in. Secret values are read from files, env vars or stdin, never argv,
and are never printed.
Environment and secret creation use the same endpoints as the claude.ai web app; they aren't a documented
public API, so if one starts failing, fall back to the web UI (claude.ai/code > environment settings).
"""
import json, os, platform, re, shutil, subprocess, sys, time, urllib.error, urllib.request

API = "https://api.anthropic.com"
BETA = "ccr-byoc-2025-07-29"
CFG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
URL_RE = re.compile(r"claude\.ai/code/((?:session_|cse_)[A-Za-z0-9_]{10,})")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def die(msg):
    sys.exit("liftoff: " + msg)


# ---------- auth (the CLI's own OAuth sign-in) ----------

def token():
    path = os.path.join(CFG_DIR, ".credentials.json")
    raw = None
    if os.path.exists(path):
        raw = open(path, encoding="utf-8").read()
    elif platform.system() == "Darwin":
        r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True)
        raw = r.stdout.strip() if r.returncode == 0 else None
    if not raw:
        die("no Claude Code sign-in found. Run `claude` once and sign in with your claude.ai account.")
    tok = (json.loads(raw).get("claudeAiOauth") or {}).get("accessToken")
    if not tok:
        die("Claude Code is signed in with an API key, not a claude.ai account. Cloud sessions need `claude auth login`.")
    return tok


def org_uuid():
    for p in (os.path.join(os.path.dirname(CFG_DIR), ".claude.json"), os.path.join(CFG_DIR, ".claude.json"),
              os.path.join(os.path.expanduser("~"), ".claude.json")):
        if os.path.exists(p):
            o = (json.load(open(p, encoding="utf-8")).get("oauthAccount") or {}).get("organizationUuid")
            if o:
                return o
    return ""


def api(path, method="GET", body=None):
    h = {"Authorization": "Bearer " + token(), "anthropic-version": "2023-06-01", "anthropic-beta": BETA,
         "x-organization-uuid": org_uuid(), "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    try:
        raw = urllib.request.urlopen(urllib.request.Request(API + path, data=data, headers=h, method=method),
                                     timeout=30).read()
        return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            die("sign-in expired. Run any `claude` command once to refresh it, then try again.")
        # never echo a request body: it may hold a secret
        die(f"{method} {path.split('?')[0]} -> {e.code}: {e.read().decode(errors='replace')[:200]}")


def opts(args):
    """--key value / --flag pairs -> dict (repeatable keys become lists)."""
    out, i = {}, 0
    while i < len(args):
        k = args[i]
        if not k.startswith("--"):
            die(f"unexpected argument: {k}")
        k = k[2:]
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            v, i = args[i + 1], i + 2
        else:
            v, i = True, i + 1
        out.setdefault(k, []).append(v)
    return out


def one(o, k, default=None):
    return o[k][-1] if k in o else default


def secret_value(o, prefix="value"):
    """A secret comes from a file, an environment variable or stdin. Never from the command line, where it would
    land in shell history, process lists and transcripts."""
    if f"{prefix}-file" in o:
        return open(one(o, f"{prefix}-file"), encoding="utf-8").read().strip()
    if f"{prefix}-env" in o:
        v = os.environ.get(one(o, f"{prefix}-env"))
        if not v:
            die(f"environment variable {one(o, f'{prefix}-env')} is empty")
        return v
    if f"{prefix}-stdin" in o:
        return sys.stdin.readline().strip()
    die(f"give the secret with --{prefix}-file <path>, --{prefix}-env <VAR> or --{prefix}-stdin")


def sid(s):
    m = re.search(r"(session_[A-Za-z0-9]+|cse_[A-Za-z0-9]+)", s)
    if not m:
        die(f"not a session id or link: {s}")
    return m.group(1).replace("cse_", "session_")


def claude_exe():
    exe = shutil.which("claude")
    if not exe:
        for c in ("~/.local/bin/claude.exe", "~/.local/bin/claude", "~/.claude/local/claude"):
            if os.path.exists(os.path.expanduser(c)):
                return os.path.expanduser(c)
        die("the `claude` CLI isn't on PATH.")
    return exe


# ---------- commands ----------

def cmd_check():
    ok = True
    def row(good, text):
        nonlocal ok
        ok &= good
        print(("  ok   " if good else "  FIX  ") + text)
    tok = None
    try:
        tok = token()
    except SystemExit as e:
        row(False, str(e))
    if tok:
        row(True, "Claude Code is signed in with a claude.ai account")
        try:
            n = len([e for e in api("/v1/environment_providers").get("environments", []) if e.get("kind") == "anthropic_cloud"])
            row(True, f"cloud sessions reachable ({n} cloud environment(s))")
        except SystemExit as e:
            row(False, str(e))
    row(bool(shutil.which("git")), "git installed")
    gh = shutil.which("gh")
    row(bool(gh), "GitHub CLI (gh) installed" if gh else "GitHub CLI (gh) missing: https://cli.github.com")
    if gh:
        r = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
        row(r.returncode == 0, "gh signed in" if r.returncode == 0 else "gh not signed in: run `gh auth login`")
    if platform.system() == "Windows":
        sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".cache", "liftoff",
                                        f"py{sys.version_info[0]}{sys.version_info[1]}"))
        try:
            import winpty  # noqa: F401
            row(True, "pywinpty available")
        except ImportError:
            print("  note pywinpty will be installed on first launch (needed on Windows)")
    sys.exit(0 if ok else 1)


def cmd_envs():
    for e in api("/v1/environment_providers").get("environments", []):
        if e.get("kind") == "anthropic_cloud" and e.get("state", "active") == "active":
            print(f"{e['environment_id']}  {e['name']}")


def cmd_env_create(name, *args):
    """env-create <name> [--host h]... [--only-listed-hosts] [--var K=V]... [--setup-file setup.sh]"""
    o = opts(args)
    hosts = [h for h in o.get("host", []) if h is not True]
    env = {}
    for kv in o.get("var", []):
        if kv is True or "=" not in kv:
            die("--var takes KEY=value")
        k, v = kv.split("=", 1)
        env[k] = v
    setup = open(one(o, "setup-file"), encoding="utf-8").read() if "setup-file" in o else None
    body = {"name": name, "kind": "anthropic_cloud", "description": "created by Liftoff",
            "config": {"environment_type": "anthropic", "cwd": "/home/user", "init_script": setup, "environment": env,
                       "languages": [{"name": "python", "version": "3.11"}, {"name": "node", "version": "20"}],
                       "network_config": {"allowed_hosts": hosts,
                                          "allow_default_hosts": "only-listed-hosts" not in o}}}
    e = api("/v1/environment_providers/cloud/create", "POST", body)
    print(f"ENV {e.get('environment_id')}  {name}")


def cmd_secret_add(env_id, host, *args):
    """secret-add <env_id> <host> [--header Authorization] [--prefix Bearer] --value-file F | --value-env VAR | --value-stdin
       secret-add <env_id> <host> --basic-user U --password-file F | --password-env VAR | --password-stdin"""
    o = opts(args)
    if host.startswith("*"):
        die("the API only takes exact hosts (api.example.com), not wildcards. Add one secret per host, or for a "
            "service that spreads across many subdomains, add a *.domain secret in the claude.ai environment settings.")
    body = {"environment_id": env_id, "host": host.lower().rstrip("."), "display_name": one(o, "name", host)}
    if "basic-user" in o:
        body["basic"] = {"username": one(o, "basic-user"), "password": secret_value(o, "password")}
    else:
        prefix = one(o, "prefix", "Bearer")
        prefix = "" if prefix in ("", "none", True) else (prefix if prefix.endswith("=") else prefix.rstrip() + " ")
        body["bearer"] = {"header_name": one(o, "header", "Authorization"), "prefix": prefix,
                          "value": secret_value(o)}
    r = api("/v1/code/agent-proxy/environment-credentials", "POST", body)
    c = r.get("credential") or {}
    print(f"SECRET {c.get('id')}  {host}  {'replaced an existing one' if r.get('replaced') else 'added'}")


def cmd_secret_list(env_id):
    for d in api(f"/v1/code/agent-proxy/environment-credentials?environment_id={env_id}").get("data", []):
        c = d["credential"]
        print(f"{c['id']}  {','.join(c.get('host_constraint') or [])}  {c.get('spec_type')}  "
              f"headers={','.join(d.get('header_names') or [])}  last used {c.get('last_used_at') or 'never'}")


def cmd_secret_delete(env_id, secret_id):
    api(f"/v1/code/agent-proxy/environment-credentials/{secret_id}?environment_id={env_id}", "DELETE")
    print(f"deleted {secret_id}")


def cmd_log(s, n="8"):
    """The last few things the cloud session said or printed."""
    ev = api(f"/v1/sessions/{sid(s)}/events?limit=200").get("data", [])
    out = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("text", "output", "stdout") and isinstance(v, str) and v.strip():
                    out.append(v.strip())
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(ev)
    for t in out[-int(n):]:
        print("---\n" + t[:1500])


def cmd_pin(project, env_id):
    path = os.path.join(project, ".claude", "settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    data.setdefault("remote", {})["defaultEnvironmentId"] = env_id
    json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
    print(f"pinned {env_id} in {path}")


def _spawn_tty(argv, cwd):
    """Run argv attached to a pseudo-terminal (claude --cloud refuses to run without one). Yields screen text."""
    if platform.system() == "Windows":
        # pywinpty goes into a private folder, so it works from any Python (venv or not) and touches nothing else.
        deps = os.path.join(os.path.expanduser("~"), ".cache", "liftoff", f"py{sys.version_info[0]}{sys.version_info[1]}")
        sys.path.insert(0, deps)
        try:
            from winpty import PtyProcess
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "--target", deps, "pywinpty"], check=False)
            import importlib
            importlib.invalidate_caches()  # the folder was empty when this run started
            try:
                from winpty import PtyProcess
            except ImportError:
                die(f"couldn't install pywinpty. Run: python -m pip install --target \"{deps}\" pywinpty")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
        p = PtyProcess.spawn(argv, cwd=cwd, dimensions=(60, 250), env=env)
        return (lambda: p.read(4096)), p.write, p.isalive, lambda: p.terminate(force=True)
    import pty, select
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        for k in [k for k in os.environ if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_")]:
            os.environ.pop(k)
        os.execvp(argv[0], argv)
    def read():
        r, _, _ = select.select([fd], [], [], 0.5)
        return os.read(fd, 4096).decode(errors="replace") if r else ""
    def alive():
        try:
            return os.waitpid(pid, os.WNOHANG) == (0, 0)
        except ChildProcessError:
            return False
    def kill():
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    return read, (lambda s: os.write(fd, s.encode())), alive, kill


def cmd_launch(project, prompt_file, timeout=240):
    import threading
    project = os.path.abspath(project)
    prompt = open(prompt_file, encoding="utf-8").read()
    pinned = False
    for f in ("settings.json", "settings.local.json"):
        p = os.path.join(project, ".claude", f)
        if os.path.exists(p) and (json.load(open(p, encoding="utf-8")).get("remote") or {}).get("defaultEnvironmentId"):
            pinned = True
    if not pinned:
        die("no cloud environment pinned for this project. Run `liftoff.py envs`, then "
            "`liftoff.py pin <project> <env_id>` (pick the default one if nothing special is needed).")
    read, write, alive, kill = _spawn_tty([claude_exe(), "--cloud", prompt], project)
    chunks = []
    def pump():
        while True:
            try:
                c = read()
            except (EOFError, OSError):
                return
            if c:
                chunks.append(c)
    threading.Thread(target=pump, daemon=True).start()
    url, trusted, end = None, False, time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        flat = re.sub(r"\s+", "", ANSI.sub("", "".join(chunks)))
        # Workspace trust prompt: the user asked to send THIS project, so trust it.
        if not trusted and "Itrustthisfolder" in flat:
            trusted = True
            time.sleep(0.5); write("\x1b[B"); time.sleep(0.4); write("\r")
        m = URL_RE.search(flat)
        if m:
            url = "https://claude.ai/code/" + m.group(1)
            time.sleep(4)  # let the CLI hand over the first message
            break
        if not alive():
            break
    kill()
    if not url:
        die("no session link appeared. Last screen:\n" + ANSI.sub("", "".join(chunks))[-1500:])
    print("SESSION", url)


def _session(s):
    return api("/v1/sessions/" + sid(s))


def _branches(d):
    out = []
    for o in (d.get("session_context") or {}).get("outcomes") or []:
        gi = o.get("git_info") or {}
        out += [(gi.get("repo"), b) for b in gi.get("branches") or []]
    return out


def cmd_status(s):
    d = _session(s)
    print(f"title:       {d.get('title')}")
    print(f"state:       {d.get('session_status')} ({d.get('connection_status')})")
    print(f"environment: {d.get('environment_id')}")
    for repo, b in _branches(d):
        print(f"branch:      {repo} @ {b}")
    print(f"link:        https://claude.ai/code/{d.get('id')}")


def cmd_send(s, message):
    r = subprocess.run([claude_exe(), "-p", message, "--cloud", sid(s)], capture_output=True, text=True)
    print((r.stdout or r.stderr).strip())


def cmd_pull(s, project="."):
    br = _branches(_session(s))
    if not br:
        die("that session hasn't pushed a branch yet. Ask it to commit and push, then pull again.")
    _, branch = br[0]
    git = lambda *a: subprocess.run(["git", "-C", project, *a], capture_output=True, text=True)
    if git("status", "--porcelain").stdout.strip():
        die("the local checkout has uncommitted changes. Commit or stash them first.")
    f = git("fetch", "origin", branch)
    if f.returncode:
        die(f"git fetch failed: {f.stderr.strip()}")
    m = git("merge", "--no-edit", "FETCH_HEAD")
    if m.returncode:
        git("merge", "--abort")
        die(f"merge conflict pulling {branch}. Resolve by hand: git -C {project} merge origin/{branch}")
    print(f"pulled {branch} into {git('branch', '--show-current').stdout.strip()}")
    print(git("log", "--oneline", "-5").stdout.strip())


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmds = {"check": (cmd_check, 0), "envs": (cmd_envs, 0), "pin": (cmd_pin, 2), "launch": (cmd_launch, 2),
            "status": (cmd_status, 1), "send": (cmd_send, 2), "pull": (cmd_pull, 1), "log": (cmd_log, 1),
            "env-create": (cmd_env_create, 1), "secret-add": (cmd_secret_add, 2), "secret-list": (cmd_secret_list, 1),
            "secret-delete": (cmd_secret_delete, 2)}
    if not a or a[0] not in cmds or len(a) - 1 < cmds[a[0]][1]:
        print(__doc__)
        sys.exit(0 if not a else 2)
    cmds[a[0]][0](*a[1:])
