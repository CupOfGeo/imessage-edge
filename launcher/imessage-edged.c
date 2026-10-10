// imessage-edged: the binary that holds Full Disk Access.
//
// macOS attributes FDA to the responsible process, so a child spawned
// here inherits this binary's grant. The Python interpreter itself gets
// no grant, which means `python -c <anything>` can't borrow it.
//
// Takes no arguments and no environment: everything an attacker could
// steer through `launchctl setenv` is fixed at build time. EDGE_PYTHON
// (venv interpreter) and EDGE_CONFIG (config.env path) are passed as
// -D string macros by build.sh, which also signs the ImessageEdge.app
// bundle with Developer ID and the hardened runtime (dyld ignores DYLD_*).
#include <errno.h>
#include <pwd.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t child;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(void) {
    // HOME from the user database, not the environment: it locates
    // chat.db and the cursor file.
    struct passwd *pw = getpwuid(getuid());
    if (pw == NULL) {
        perror("getpwuid");
        return 1;
    }
    char home[1024];
    snprintf(home, sizeof home, "HOME=%s", pw->pw_dir);
    char *envp[] = {
        home,
        "PATH=/usr/bin:/bin",
        "IMESSAGE_EDGE_CONFIG=" EDGE_CONFIG,
        NULL,
    };
    char *argv[] = {EDGE_PYTHON, "-I", "-m", "imessage_edge", NULL};

    signal(SIGTERM, forward);
    int rc = posix_spawn(&child, EDGE_PYTHON, NULL, NULL, argv, envp);
    if (rc != 0) {
        errno = rc;
        perror("posix_spawn " EDGE_PYTHON);
        return 127;
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("waitpid");
            return 1;
        }
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
