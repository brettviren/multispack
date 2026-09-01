// Only run for the cxxstd=23 flavour: <expected> needs libstdc++ 13+, so this
// checks that the C++ runtime we ship is the one being used, not the host's.
#include <cstdio>
#if __cplusplus >= 202302L && __has_include(<expected>)
#include <expected>
#include <string>
void cxx23lib()
{
    std::expected<int, std::string> good{42};
    std::expected<int, std::string> bad{std::unexpect, "nope"};
    printf("CXX23LIB %d %d\n", good.value(), (int)!bad.has_value());
}
#else
void cxx23lib() { printf("CXX23LIB unavailable\n"); }
#endif
