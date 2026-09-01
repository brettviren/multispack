/* Minimal pthread exerciser for the glibc symbol-version demonstration.
 *
 * It calls a handful of the pthread functions that moved from libpthread into
 * libc at glibc 2.34.  Compiled against a given distro's glibc, the binary's
 * dynamic symbol table records WHICH versioned pthread symbols it requires
 * (readelf --dyn-syms), and running it on another distro shows whether that
 * distro's glibc exports those versions.  See README.md.
 */
#include <pthread.h>
#include <stdio.h>

static void  *worker(void *arg) { return arg; }
static void   once_fn(void)     { }
static pthread_once_t once = PTHREAD_ONCE_INIT;

int main(void)
{
    pthread_t t;
    pthread_key_t k;

    pthread_create(&t, 0, worker, 0);
    pthread_join(t, 0);
    pthread_key_create(&k, 0);
    pthread_once(&once, once_fn);

    puts("PTHREAD_OK");
    return 0;
}
