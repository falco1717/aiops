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
 * Two modes, both refused to anyone but the application's own uid:
 *
 *   aiops-runas <command> [args...]     drop to the agent user, then exec
 *   aiops-runas --kill-group <sig> <pgid>
 *   aiops-runas --kill <sig> <pid>      signal agent processes only
 *
 * The signalling mode is here because the switch it performs is genuinely
 * one-way: once an agent runs under its own uid, the app cannot signal it any
 * more (EPERM), and cancelling a run is not optional. It will only ever signal
 * a process that is already running as the agent user, and only with the three
 * signals a supervisor needs.
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
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

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

#define EXIT_USAGE 2
#define EXIT_DENIED 3
#define EXIT_FAILED 4
#define EXIT_NOTHING 5

static int fail(const char *what)
{
    fprintf(stderr, "aiops-runas: %s: %s\n", what, strerror(errno));
    return EXIT_FAILED;
}

/* Become the agent user irreversibly, or do not continue. */
static int drop_privileges(void)
{
    gid_t gid = (gid_t)AIOPS_AGENT_GID;
    uid_t uid = (uid_t)AIOPS_AGENT_UID;

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
        fprintf(stderr, "aiops-runas: identity is not the agent's after dropping\n");
        return EXIT_FAILED;
    }
    /* Group-writable by default: everything an agent creates in a workspace or
     * a credential directory has to stay usable by the application. */
    umask(002);
    return 0;
}

static int parse_signal(const char *text)
{
    long value = strtol(text, NULL, 10);
    if (value == SIGTERM || value == SIGKILL || value == SIGINT)
        return (int)value;
    return -1;
}

static int owned_by_agent(pid_t pid)
{
    char path[64];
    struct stat info;

    snprintf(path, sizeof(path), "/proc/%ld", (long)pid);
    if (stat(path, &info) != 0)
        return 0;
    return info.st_uid == (uid_t)AIOPS_AGENT_UID;
}

/* Signal one agent process, or every agent process in one group. Anything not
 * running as the agent user is skipped, so this cannot be turned on the app. */
static int signal_agents(int sig, pid_t target, int by_group)
{
    DIR *proc;
    struct dirent *entry;
    int hit = 0;

    if (!by_group) {
        if (!owned_by_agent(target)) {
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
        if (!owned_by_agent(pid))
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
                "       aiops-runas --kill[-group] <signal> <pid>\n");
        return EXIT_USAGE;
    }

    /* Only the application may use this. The agent user can execute the binary
     * — it is on a filesystem it can reach — but gains nothing by it: exec mode
     * would put it where it already is, and signalling is not its business. */
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
        int dropped = drop_privileges();
        if (dropped != 0)
            return dropped;
    }
    execvp(argv[1], &argv[1]);
    fprintf(stderr, "aiops-runas: exec %s: %s\n", argv[1], strerror(errno));
    return 127;
}
