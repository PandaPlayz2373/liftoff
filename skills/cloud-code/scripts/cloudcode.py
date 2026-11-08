#!/usr/bin/env python3
"""Cloud Code helper: move a local Claude Code session's project to a cloud session and back.

  cloudcode.py check                         sign-in, git, GitHub CLI and cloud access all OK?
  cloudcode.py envs                          your cloud environments (id, name)
  cloudcode.py pin <project> <env_id>        make cloud sessions from this project use that environment
  cloudcode.py launch <project> <prompt.md>  start a cloud session with that first message; prints SESSION <url>
  cloudcode.py status <session>              a session's state, environment, repo and branch
  cloudcode.py send <session> "<message>"    queue a follow-up message into a running cloud session
  cloudcode.py pull <session> [<project>]    bring the cloud session's commits back into the local checkout

Stdlib only, except `launch` on Windows, which needs pywinpty (installed on first use).
Uses the Claude Code CLI's own sign-in; it never asks for or stores a key.
"""
import json, os, platform, re, shutil, subprocess, sys, time, urllib.error, urllib.request

API = "https://api.anthropic.com"
BETA = "ccr-byoc-2025-07-29"
CFG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
URL_RE = re.compile(r"claude\.ai/code/((?:session_|cse_)[A-Za-z0-9_]{10,})")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def die(msg):
    sys.exit("cloud-code: " + msg)


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


def api(path):
    h = {"Authorization": "Bearer " + token(), "anthropic-version": "2023-06-01", "anthropic-beta": BETA,
         "x-organization-uuid": org_uuid()}
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(API + path, headers=h), timeout=30).read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            die("sign-in expired. Run any `claude` command once to refresh it, then try again.")
        die(f"{path} -> {e.code}: {e.read().decode(errors='replace')[:200]}")


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
        sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".cache", "cloud-code",
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
        deps = os.path.join(os.path.expanduser("~"), ".cache", "cloud-code", f"py{sys.version_info[0]}{sys.version_info[1]}")
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
            "status": (cmd_status, 1), "send": (cmd_send, 2), "pull": (cmd_pull, 1)}
    if not a or a[0] not in cmds or len(a) - 1 < cmds[a[0]][1]:
        print(__doc__)
        sys.exit(0 if not a else 2)
    cmds[a[0]][0](*a[1:])
