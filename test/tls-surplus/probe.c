/* probe.c -- dlopen() shared libraries one by one until it fails, and report how
 * many succeeded.  Models a plugin host (like Phlex or Wire-Cell Toolkit) that
 * dlopen()s many component libraries at startup.
 *
 * A library carrying an INITIAL-EXEC thread-local block must be placed in the
 * per-thread STATIC TLS block at dlopen() time; glibc reserves only a small fixed
 * "static TLS surplus" (default ~2 KB) for that, so a library whose initial-exec
 * TLS exceeds it cannot be dlopen()ed at all:
 *     dlopen(...): cannot allocate memory in static TLS block
 * A library built with the default GLOBAL-DYNAMIC model has no such limit.
 *
 * Usage: probe <dir> <prefix> <max>
 *   dlopens <dir>/<prefix>NNN.so for NNN = 000..max-1 and prints "OPENED <n>".
 */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    const char *dir    = argc > 1 ? argv[1] : ".";
    const char *prefix = argc > 2 ? argv[2] : "ie-lib";
    int         max    = argc > 3 ? atoi(argv[3]) : 32;
    int opened = 0;

    for (int i = 0; i < max; i++) {
        char path[512];
        snprintf(path, sizeof path, "%s/%s%03d.so", dir, prefix, i);
        void *h = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
        if (!h) {
            fprintf(stderr, "  dlopen #%d (%s) failed: %s\n", i, path, dlerror());
            break;
        }
        opened++;
    }
    printf("OPENED %d\n", opened);
    return 0;
}
