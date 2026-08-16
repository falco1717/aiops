/*
 * aiops-runas — start an AIOps agent process as the agent user, and signal it
 * afterwards.
 *
 * Why this exists at all: the control plane must not be reachable from an
 * agent, and an agent that shares a uid with the control plane always is. It
 * can read /proc/<app-pid>/environ and recover every variable the app was
 * careful not to pass down, it can ptrace the app, and it can read whatever
 * the app can read. A separate uid closes all of that at once, but switching
 * uid needs privilege the app deliberately does not have, so it is delegated
 * to this: forty lines that hold root for a handful of syscalls and then are
 * not root any more.
 *
 * Four modes:
 *
 *   aiops-runas <command> [args...]     drop to the agent user, then exec
 *   aiops-runas --kill-group <sig> <pgid>
 *   aiops-runas --kill <sig> <pid>      signal isolated processes only
 *   aiops-runas --as-browser <command> [args...]
 *                                       drop to the *browser* user, then exec
 *
 * The first three are refused to anyone but the application's own uid. The
 * browser mode is also allowed to the agent, because that is who asks for it.
 *
 * The signalling mode is here because the switch it performs is genuinely
 * one-way: once an agent runs under its own uid, the app cannot signal it any
 * more (EPERM), and cancelling a run is not optional. It will only ever signal
 * a process that is already running as the agent or the browser user, and only
 * with the three signals a supervisor needs.
 *
 * -- why a third user ---------------------------------------------------
 *
 * The agent's browser renders pages nobody vetted. A renderer exploit is code
 * execution, and until this existed that code ran as the agent — which is to
 * say with read access to the run's decrypted SSH private keys (group-readable
 * to the agent by design, because `ssh` has to load them), to the stored
 * passwords in AIOPS_SSHPASS_*, and to the relay token. Chromium's own sandbox
 * does not close that: Docker's default seccomp profile blocks the user
 * namespace it needs, so in this container it is off.
 *
 * So the browser gets a uid of its own, in a group of its own, sharing nothing
 * with the agent. It is a strictly *lower* privilege than the agent — this
 * mode only ever moves downwards — which is why the agent is allowed to ask
 * for it. The environment is swept here rather than only in Python, because
 * the process asking is the one on the other side of the boundary and a
 * chokepoint it can skip is not one.
 *
 * Built static (see the Dockerfile) so the dynamic loader is not part of a
 * setuid binary's attack surface, and with the uids fixed at compile time so
 * nothing is looked up in a file an attacker might reach.
 */

/* setresuid/setresgid are glibc extensions; without this they are not declared
 * and the compiler silently assumes an int-returning function. */
#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

extern char **environ;

/* uid the agent runs as; gid it shares with the application. */
#ifndef AIOPS_AGENT_UID
#define AIOPS_AGENT_UID 1001
#endif
#ifndef AIOPS_AGENT_GID
#define AIOPS_AGENT_GID 1000
#endif
/* uid the application runs as: the only caller this program serves. */
#ifndef AIOPS_APP_UID
#define AIOPS_APP_UID 1000
#endif
/* uid and gid the browser runs as. Deliberately its *own* group: the agent's
 * group is what makes a run's SSH keys readable, and the browser must not be
 * in it. Nothing else in the image belongs to this group. */
#ifndef AIOPS_BROWSER_UID
#define AIOPS_BROWSER_UID 1002
#endif
#ifndef AIOPS_BROWSER_GID
#define AIOPS_BROWSER_GID 1002
#endif
/* Where the browser user may write. Its own home, mode 0700, owned by it and
 * reachable by nobody else — Chromium needs a profile and a scratch directory
 * and must not be given the agent's. */
#ifndef AIOPS_BROWSER_HOME
#define AIOPS_BROWSER_HOME "/home/aiops-browser"
#endif

#define EXIT_USAGE 2
#define EXIT_DENIED 3
#define EXIT_FAILED 4
#define EXIT_NOTHING 5

static int fail(const char *what)
{
    fprintf(stderr, "aiops-runas: %s: %s\n", what, strerror(errno));
    return EXIT_FAILED;
}

/* Become another user irreversibly, or do not continue. */
static int drop_privileges(uid_t uid, gid_t gid, mode_t mask)
{
    if (uid == 0 || gid == 0) {
        fprintf(stderr, "aiops-runas: built with a root target\n");
        return EXIT_FAILED;
    }
    /* Supplementary groups first: it is the last thing that still needs root. */
    if (setgroups(1, &gid) != 0)
        return fail("setgroups");
    if (setresgid(gid, gid, gid) != 0)
        return fail("setresgid");
    if (setresuid(uid, uid, uid) != 0)
        return fail("setresuid");

    /* Prove it took. A saved-set-uid left behind would let the agent walk back
     * to the application's user, which is the whole thing we are preventing. */
    if (setuid(0) == 0 || seteuid(0) == 0 || setgid(0) == 0) {
        fprintf(stderr, "aiops-runas: privileges were not dropped\n");
        return EXIT_FAILED;
    }
    if (getuid() != uid || geteuid() != uid || getgid() != gid || getegid() != gid) {
        fprintf(stderr, "aiops-runas: identity is not the target's after dropping\n");
        return EXIT_FAILED;
    }
    umask(mask);
    return 0;
}

/* Names and prefixes the browser must never inherit.
 *
 * The same list as agent_env.py's, minus its one re-added name: an agent
 * legitimately needs AIOPS_WORKSPACE_ROOT and a browser needs nothing at all
 * from AIOps. What this actually removes in practice is the run's own
 * credentials — AIOPS_SSHPASS_* holds a stored system's password, AIOPS_ASKPASS_*
 * names a program that prints a key's passphrase, AIOPS_RELAY_TOKEN opens
 * streams through a node and AIOPS_APPROVAL_TOKEN speaks to the app's loopback
 * API. They are added to the agent's environment per run, so they are present
 * in the process that asks for a browser, and none of them are the browser's
 * business.
 */
static int blocked_variable(const char *entry, size_t len)
{
    static const char *const prefixes[] = { "AIOPS_", "POSTGRES_", "PG", NULL };
    static const char *const names[] = {
        "DATABASE_URL", "SECRET_KEY", "JWT_SECRET", "ADMIN_PASSWORD", NULL
    };
    int i;

    for (i = 0; prefixes[i] != NULL; i++) {
        size_t plen = strlen(prefixes[i]);
        if (len >= plen && strncmp(entry, prefixes[i], plen) == 0)
            return 1;
    }
    for (i = 0; names[i] != NULL; i++) {
        if (len == strlen(names[i]) && strncmp(entry, names[i], len) == 0)
            return 1;
    }
    return 0;
}

/* Strip them, then say where the browser may write.
 *
 * Restarted from the top after each removal because unsetenv() rewrites the
 * array underneath any iterator. The environment is a handful of entries; the
 * cost is not worth an optimisation that would be wrong.
 */
static int sweep_environment(void)
{
    int again = 1;

    while (again) {
        char **entry;
        again = 0;
        for (entry = environ; *entry != NULL; entry++) {
            const char *equals = strchr(*entry, '=');
            size_t len = equals != NULL ? (size_t)(equals - *entry) : strlen(*entry);
            char name[128];

            if (!blocked_variable(*entry, len))
                continue;
            if (len >= sizeof(name))
                len = sizeof(name) - 1;
            memcpy(name, *entry, len);
            name[len] = '\0';
            if (unsetenv(name) != 0)
                return fail("unsetenv");
            again = 1;
            break;
        }
    }

    /* HOME would otherwise still point at the agent's, which the browser
     * cannot write and should not read. Chromium puts its profile under
     * TMPDIR (Playwright makes it there), so both are moved together. */
    if (setenv("HOME", AIOPS_BROWSER_HOME, 1) != 0)
        return fail("setenv HOME");
    if (setenv("TMPDIR", AIOPS_BROWSER_HOME "/tmp", 1) != 0)
        return fail("setenv TMPDIR");
    return 0;
}

static int parse_signal(const char *text)
{
    long value = strtol(text, NULL, 10);
    if (value == SIGTERM || value == SIGKILL || value == SIGINT)
        return (int)value;
    return -1;
}

/* True for a process on the far side of the boundary — an agent's, or the
 * browser stack an agent started. The browser is included because a cancelled
 * run has to take the whole tree with it: Playwright's driver is a child of the
 * agent's MCP bridge and sits in the agent's process group, but it runs as the
 * browser user, so a check for the agent's uid alone would step over exactly
 * the process that holds a Chromium open. */
static int owned_by_isolated_user(pid_t pid)
{
    char path[64];
    struct stat info;

    snprintf(path, sizeof(path), "/proc/%ld", (long)pid);
    if (stat(path, &info) != 0)
        return 0;
    return info.st_uid == (uid_t)AIOPS_AGENT_UID || info.st_uid == (uid_t)AIOPS_BROWSER_UID;
}

/* Signal one such process, or every one of them in a group. Anything running
 * as somebody else is skipped, so this cannot be turned on the app. */
static int signal_agents(int sig, pid_t target, int by_group)
{
    DIR *proc;
    struct dirent *entry;
    int hit = 0;

    if (!by_group) {
        if (!owned_by_isolated_user(target)) {
            fprintf(stderr, "aiops-runas: %ld is not an agent process\n", (long)target);
            return EXIT_NOTHING;
        }
        if (kill(target, sig) != 0)
            return fail("kill");
        return 0;
    }

    proc = opendir("/proc");
    if (proc == NULL)
        return fail("opendir /proc");
    while ((entry = readdir(proc)) != NULL) {
        pid_t pid = (pid_t)strtol(entry->d_name, NULL, 10);
        if (pid <= 1)
            continue;
        if (!owned_by_isolated_user(pid))
            continue;
        if (getpgid(pid) != target)
            continue;
        if (kill(pid, sig) == 0)
            hit++;
    }
    closedir(proc);
    if (hit == 0) {
        fprintf(stderr, "aiops-runas: no agent process in group %ld\n", (long)target);
        return EXIT_NOTHING;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr,
                "usage: aiops-runas <command> [args...]\n"
                "       aiops-runas --as-browser <command> [args...]\n"
                "       aiops-runas --kill[-group] <signal> <pid>\n");
        return EXIT_USAGE;
    }

    /* The browser mode first, because it is the one with a second caller. The
     * agent asks for it (Playwright's driver is started through this), and the
     * application is allowed too so that it can probe the identity at boot and
     * so the test suite can measure what the browser user can actually reach.
     * The browser user itself is refused: it is already there, and letting a
     * compromised renderer re-enter this program buys it nothing but does make
     * the reachable surface larger for no reason. */
    if (strcmp(argv[1], "--as-browser") == 0) {
        int dropped;

        if (argc < 3) {
            fprintf(stderr, "usage: aiops-runas --as-browser <command> [args...]\n");
            return EXIT_USAGE;
        }
        if (getuid() != (uid_t)AIOPS_APP_UID && getuid() != (uid_t)AIOPS_AGENT_UID) {
            fprintf(stderr, "aiops-runas: not permitted for uid %ld\n", (long)getuid());
            return EXIT_DENIED;
        }
        /* 077: nothing the browser writes is shared with anybody. Screenshots
         * do not come this way — Playwright's client writes those, in the
         * agent's own process — so there is no file here anyone else needs. */
        dropped = drop_privileges((uid_t)AIOPS_BROWSER_UID, (gid_t)AIOPS_BROWSER_GID, 077);
        if (dropped != 0)
            return dropped;
        dropped = sweep_environment();
        if (dropped != 0)
            return dropped;
        /* A browser must not outlive the bridge that is answerable for it.
         * Playwright's driver normally exits when its stdin closes, but a
         * bridge that was killed rather than closed leaves nobody holding the
         * pipe — and the app cannot signal this process any more, because it
         * is about to stop being the agent's uid as well as the app's.
         *
         * Set after the credential change, because execve() of a set-uid
         * program clears it and this program is one. The target is not, so it
         * survives the exec below. */
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0)
            return fail("prctl PR_SET_PDEATHSIG");
        if (getppid() == 1) {
            fprintf(stderr, "aiops-runas: the parent is already gone\n");
            return EXIT_FAILED;
        }
        execvp(argv[2], &argv[2]);
        fprintf(stderr, "aiops-runas: exec %s: %s\n", argv[2], strerror(errno));
        return 127;
    }

    /* Everything else is the application's alone. The agent user can execute
     * the binary — it is on a filesystem it can reach — but gains nothing by
     * it: exec mode would put it where it already is, and signalling is not
     * its business. */
    if (getuid() != (uid_t)AIOPS_APP_UID) {
        fprintf(stderr, "aiops-runas: not permitted for uid %ld\n", (long)getuid());
        return EXIT_DENIED;
    }

    if (strcmp(argv[1], "--kill") == 0 || strcmp(argv[1], "--kill-group") == 0) {
        int by_group = strcmp(argv[1], "--kill-group") == 0;
        int sig;
        pid_t target;

        if (argc != 4) {
            fprintf(stderr, "usage: aiops-runas %s <signal> <pid>\n", argv[1]);
            return EXIT_USAGE;
        }
        sig = parse_signal(argv[2]);
        if (sig < 0) {
            fprintf(stderr, "aiops-runas: %s is not a relayable signal\n", argv[2]);
            return EXIT_USAGE;
        }
        target = (pid_t)strtol(argv[3], NULL, 10);
        if (target <= 1) {
            fprintf(stderr, "aiops-runas: refusing to signal %s\n", argv[3]);
            return EXIT_USAGE;
        }
        return signal_agents(sig, target, by_group);
    }

    if (argv[1][0] == '-') {
        fprintf(stderr, "aiops-runas: unknown option %s\n", argv[1]);
        return EXIT_USAGE;
    }

    {
        /* Group-writable by default: everything an agent creates in a
         * workspace or a credential directory has to stay usable by the
         * application. */
        int dropped = drop_privileges(
            (uid_t)AIOPS_AGENT_UID, (gid_t)AIOPS_AGENT_GID, 002);
        if (dropped != 0)
            return dropped;
    }
    execvp(argv[1], &argv[1]);
    fprintf(stderr, "aiops-runas: exec %s: %s\n", argv[1], strerror(errno));
    return 127;
}
