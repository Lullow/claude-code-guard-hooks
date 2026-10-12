#!/usr/bin/env python3
"""Säkerhetsnät för Claude Code: en PreToolUse-hook som spärrar farliga tool-calls.

Claude Code skickar varje tool-call som JSON på stdin innan den körs.
Exit 2 stoppar anropet, och texten på stderr visas för Claude som förklaring.
Exit 0 släpper igenom. Ett tredje svar, "fråga användaren", skrivs som JSON på
stdout med exit 0: Claude Code visar då en fråga och användaren bestämmer.
Trasig indata eller en bugg i hooken stoppar anropet (fail-closed), eftersom
ett kommando som får hooken att krascha annars skulle slinka igenom.

Reglerna gäller tre saker:
  1. git-kommandon som skriver om historik eller förstör osparat arbete
  2. ändringar i själva säkerhetsnätet (settings-filer och installerade hooks)
  3. Docker- och k8s-kommandon som raderar data, noder eller hela kluster
"""
import json
import os
import shlex
import sys
from pathlib import Path

TAIL = ("Försök inte ta dig runt spärren. Behövs kommandot verkligen: "
        "be användaren köra det själv med `! <kommando>`.")

HOOKS_DIR = Path.home() / ".claude" / "hooks"
SETTINGS_NAMES = {"settings.json", "settings.local.json"}
READ_ONLY = {"cat", "less", "head", "tail", "grep", "ls", "stat", "wc", "diff", "cmp", "md5sum",
             "sha256sum", "file", "jq", "realpath", "readlink", "du"}
SHELLS = {"bash", "sh", "zsh", "dash"}


class Ask(str):
    """Ett skäl som ska bli en fråga till användaren i stället för ett nej."""

GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
DOCKER_OPTS_WITH_VALUE = {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level"}
COMPOSE_OPTS_WITH_VALUE = {"-f", "--file", "-p", "--project-name", "--profile",
                           "--env-file", "--project-directory"}
KUBECTL_OPTS_WITH_VALUE = {"-n", "--namespace", "--context", "--kubeconfig", "--cluster",
                           "--user", "-s", "--server", "-f", "--filename", "-l", "--selector",
                           "-o", "--output"}
KUBECTL_PROTECTED_KINDS = {"namespace", "namespaces", "ns", "node", "nodes", "no"}


# --- Uppdelning av kommandot ---------------------------------------------------

def tokenize(command):
    """Delar upp kommandot i ord. Operatorer (&&, ;, |) och radbrytningar blir egna ord."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # t.ex. ett citattecken som aldrig stängs
        return command.split()


def is_separator(word):
    """Sant för ord som avslutar ett kommando: ; && || | & ( ) och radbrytning."""
    return (set(word) <= set(";&|()<>\n")
            and (word in ("&", "&&") or any(c in word for c in ";|()\n")))


def is_redirect(word):
    return set(word) <= set("<>&") and ">" in word


def segments(words):
    """Delar ordlistan i enskilda kommandon."""
    current = []
    for word in words:
        if is_separator(word):
            if current:
                yield current
            current = []
        else:
            current.append(word)
    if current:
        yield current


def split_subcommand(args, opts_with_value):
    """Hoppar över globala flaggor (t.ex. `git -C <dir>`) och returnerar (underkommando, resten)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in opts_with_value else 1
    if i >= len(args):
        return None, []
    return args[i], args[i + 1:]


def positionals(args, opts_with_value):
    """Orden som inte är flaggor eller värden till flaggor."""
    out, i = [], 0
    while i < len(args):
        if args[i].startswith("-"):
            i += 2 if args[i] in opts_with_value else 1
        else:
            out.append(args[i])
            i += 1
    return out


def has_flag(args, short="", long=()):
    """Sant om någon flagga finns, även ihopskrivna korta flaggor som `-uf`."""
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("--"):
            if arg.split("=")[0] in long:
                return True
        elif arg.startswith("-") and any(c in arg[1:] for c in short):
            return True
    return False


# --- Regler per program --------------------------------------------------------

def check_git(args):
    sub, rest = split_subcommand(args, GIT_OPTS_WITH_VALUE)
    if sub == "push":
        if has_flag(rest, "fd", ("--force", "--force-with-lease", "--force-if-includes",
                                 "--delete", "--mirror", "--prune")):
            return "`git push` med force eller delete skriver om eller raderar historik på remote"
        if any(a.startswith(("+", ":")) for a in rest):
            return "en refspec med `+` (force) eller `:` (radera) skriver om eller raderar en gren på remote"
    if sub == "reset":
        moves_branch = any(c in a for a in rest if not a.startswith("-") for c in ("~", "^", "@{"))
        if has_flag(rest, "", ("--hard", "--soft", "--merge", "--keep")) or moves_branch:
            return "`git reset` som flyttar grenen eller slänger ändringar skriver om historik"
    if sub == "rebase":  # lokalt och går att ångra via reflog: fråga i stället för att neka
        return Ask("`git rebase` skriver om historik")
    if sub == "commit" and has_flag(rest, "", ("--amend",)):
        return Ask("`git commit --amend` skriver om den senaste commiten")
    if sub in ("filter-branch", "filter-repo", "update-ref", "prune"):
        return f"`git {sub}` skriver om eller raderar historik"
    if sub == "branch" and has_flag(rest, "DfM", ("--force",)):
        return "`git branch -D/-f/-M` raderar eller flyttar en gren med tvång"
    if sub == "tag" and has_flag(rest, "df", ("--delete", "--force")):
        return "`git tag -d/-f` raderar eller flyttar en tagg"
    if sub == "reflog" and rest[:1] in (["expire"], ["delete"]):
        return "att rensa reflog tar bort sista chansen att återställa commits"
    if sub == "gc" and has_flag(rest, "", ("--prune",)):
        return "`git gc --prune` raderar commits som inte längre nås"
    if sub == "clean" and has_flag(rest, "f", ("--force",)):
        return "`git clean -f` raderar ospårade filer för gott"
    if sub == "checkout":
        discards = "." in rest or ("--" in rest and rest[-1] != "--")
        if discards or has_flag(rest, "fB", ("--force",)):
            return "`git checkout` på filer slänger osparade ändringar"
    if sub == "switch" and has_flag(rest, "fC", ("--force", "--discard-changes", "--force-create")):
        return "`git switch` med tvång slänger osparade ändringar"
    if sub == "restore":
        only_unstage = (has_flag(rest, "S", ("--staged",))
                        and not has_flag(rest, "W", ("--worktree",)))
        if not only_unstage:
            return "`git restore` på arbetskatalogen slänger osparade ändringar"
    if sub == "stash" and rest[:1] in (["drop"], ["clear"]):
        return "`git stash drop/clear` raderar sparat arbete"
    return None


def check_docker(args):
    sub, rest = split_subcommand(args, DOCKER_OPTS_WITH_VALUE)
    if sub == "system" and rest[:1] == ["prune"]:
        return "`docker system prune` raderar images, containrar och nätverk"
    if sub == "volume" and rest[:1] in (["rm"], ["remove"], ["prune"]):
        return "att ta bort Docker-volymer raderar data för gott"
    if sub == "compose":
        compose_sub, compose_rest = split_subcommand(rest, COMPOSE_OPTS_WITH_VALUE)
        if compose_sub == "down" and has_flag(compose_rest, "v", ("--volumes",)):
            return "`docker compose down -v` raderar projektets volymer"
    return None


def check_docker_compose(args):
    return ("`docker-compose` (med bindestreck) är den gamla varianten och används inte här. "
            "Använd `docker compose` (med mellanslag) i stället")


def check_kubectl(args):
    sub, rest = split_subcommand(args, KUBECTL_OPTS_WITH_VALUE)
    if sub == "drain":
        return "`kubectl drain` tömmer en nod på alla poddar"
    if sub == "delete":
        if has_flag(rest, "A", ("--all", "--all-namespaces")):
            return "`kubectl delete --all` raderar alla resurser av en sort"
        for spec in positionals(rest, KUBECTL_OPTS_WITH_VALUE):
            kinds = spec.split("/")[0].split(",")
            if KUBECTL_PROTECTED_KINDS & set(kinds):
                return "att radera en namespace eller en nod river allt som körs där"
    return None


def check_k3d(args):
    sub, rest = split_subcommand(args, set())
    if sub in ("cluster", "node", "registry") and rest[:1] in (["delete"], ["rm"]):
        return f"`k3d {sub} delete` river klustret eller delar av det"
    return None


def check_install_guard(args):
    return "säkerhetsnätet installeras bara av användaren själv"


CHECKERS = {
    "git": check_git,
    "docker": check_docker,
    "docker-compose": check_docker_compose,
    "kubectl": check_kubectl,
    "k3d": check_k3d,
    "install-guard.sh": check_install_guard,
}
OPTS_WITH_VALUE = {"git": GIT_OPTS_WITH_VALUE, "docker": DOCKER_OPTS_WITH_VALUE,
                   "kubectl": KUBECTL_OPTS_WITH_VALUE}


def check_variable_subcommand(name, args):
    """`git $X`: ett underkommando i en variabel går inte att granska."""
    sub, _ = split_subcommand(args, OPTS_WITH_VALUE.get(name, set()))
    if sub and sub.startswith("$"):
        return f"`{name} {sub}` har underkommandot i en variabel och går inte att granska"
    return None


def check_shell(args):
    """`bash -c "..."` granskas som ett kommando. Ett skal utan skript, som i `... | bash`,
    kör vad som helst från stdin och går inte att granska."""
    for i, arg in enumerate(args[:-1]):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
            return check_bash(args[i + 1])
    if not any(not arg.startswith("-") for arg in args):
        return "ett skal utan skript (som i `... | bash`) kör vad som helst från stdin"
    return None


# --- Säkerhetsnätets egna filer ------------------------------------------------

def mentions_protected(word):
    if " " in word:  # ett commit-meddelande eller liknande, inte en sökväg
        return False
    return ".claude/hooks" in word or os.path.basename(word.rstrip("/")) in SETTINGS_NAMES


def writes_protected(words):
    """Sant om en omdirigering (> eller >>) pekar på en skyddad fil. `2>&1` och `> /tmp/x` gör inte det."""
    return any(is_redirect(word) and i + 1 < len(words) and mentions_protected(words[i + 1])
               for i, word in enumerate(words))


def reads_only(words):
    """Sant för `cat`, `grep`, `ls` och liknande, så länge inget omdirigeras till en skyddad fil.
    Ett sådant kommando får nämna vad som helst: `cat install-guard.sh`, `grep docker-compose x`."""
    return os.path.basename(words[0]) in READ_ONLY and not writes_protected(words)


def check_protected_mention(words):
    """Bash får läsa settings och hooks, men inte ändra dem."""
    if any(mentions_protected(w) for w in words):
        return "kommandot kan ändra säkerhetsnätet (settings.json eller ~/.claude/hooks)"
    return None


def is_protected_path(path, cwd):
    p = (Path(cwd) / os.path.expanduser(path)).resolve()
    if p.name in SETTINGS_NAMES and p.parent.name == ".claude":
        return True
    return p == HOOKS_DIR or HOOKS_DIR in p.parents


# --- Ingång --------------------------------------------------------------------

def check_segment(words):
    """Letar efter bevakade program var som helst i kommandot, t.ex. efter `sudo`."""
    for i, word in enumerate(words):
        name = os.path.basename(word)
        rest = words[i + 1:]
        reason = None
        if name in SHELLS:  # bash -c "git push -f", eller ... | bash
            reason = check_shell(rest)
        elif name == "eval":
            reason = check_bash(" ".join(rest))
        elif name in CHECKERS:
            reason = check_variable_subcommand(name, rest) or CHECKERS[name](rest)
        if reason:
            return reason
    return None


def check_bash(command):
    for words in segments(tokenize(command)):
        if reads_only(words):
            continue
        reason = check_protected_mention(words) or check_segment(words)
        if reason:
            return reason
    return None


def check(tool_name, tool_input, cwd="."):
    """Returnerar en förklaring om anropet ska stoppas, annars None."""
    if tool_name == "Bash":
        return check_bash(tool_input.get("command", ""))
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path and is_protected_path(path, cwd):
            return "filen hör till säkerhetsnätet (settings.json eller ~/.claude/hooks)"
    return None


def main():
    try:
        call = json.load(sys.stdin)
        reason = check(call.get("tool_name", ""), call.get("tool_input") or {}, call.get("cwd") or ".")
    except Exception as error:  # trasig indata eller en bugg: stoppa hellre än att släppa igenom
        print(f"Stoppad av säkerhetsnätet: internt fel i hooken, {error!r}.\n{TAIL}", file=sys.stderr)
        return 2
    if isinstance(reason, Ask):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"Säkerhetsnätet frågar: {reason}."}}, ensure_ascii=False))
        return 0
    if reason:
        print(f"Stoppad av säkerhetsnätet: {reason}.\n{TAIL}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
