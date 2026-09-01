// Report the C++ standard cling is configured for, and prove the shipped
// libstdc++ actually has the C++23 library pieces (not just the -std flag).
#include <cstdio>

void cxxstd()
{
    printf("CXXSTD %ld\n", (long)__cplusplus);
#if __cplusplus >= 202302L
    printf("HAS_CXX23 1\n");
#else
    printf("HAS_CXX23 0\n");
#endif
}
