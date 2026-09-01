/* tlslib.c -- a shared library carrying a thread-local storage (TLS) block.
 *
 * TLS = Thread-Local Storage (the __thread / C++ thread_local storage class:
 * a variable with one instance per thread).  NOTHING to do with SSL/TLS.
 *
 * Built with -DIE_TLS the block uses the INITIAL-EXEC TLS model, which the
 * dynamic loader must place in the per-thread STATIC TLS block when the library
 * is dlopen()ed.  glibc reserves only a small fixed "static TLS surplus" for
 * this; enough initial-exec libraries exhaust it and dlopen() fails with
 *   "cannot allocate memory in static TLS block".
 *
 * Built WITHOUT -DIE_TLS the block uses the default GLOBAL-DYNAMIC model, which
 * is allocated lazily/dynamically and has no such fixed limit -- so any number
 * can be dlopen()ed.  That contrast is the whole point of the test.
 */
#ifndef TLS_BYTES
#define TLS_BYTES 512
#endif

#ifdef IE_TLS
#  define TLSMODEL __attribute__((tls_model("initial-exec")))
#else
#  define TLSMODEL   /* default: global-dynamic */
#endif

__thread volatile unsigned char tls_block[TLS_BYTES] TLSMODEL;

/* Referencing tls_block with the chosen model forces the relocation that makes
 * the loader account for this module's TLS at dlopen() time. */
unsigned long tls_touch(void)
{
    tls_block[0]++;
    return (unsigned long) sizeof tls_block;
}
