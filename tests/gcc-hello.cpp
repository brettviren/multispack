// Compiled and run inside a bare validator by run-compiler-validation.sh, using
// only the shipped GCC and its shipped libstdc++/libgcc_s (via rpath -- no
// LD_LIBRARY_PATH).  Exercises the standard library at the requested -std level
// and prints a single token the harness greps for.
#include <iostream>
#include <numeric>
#include <string>
#include <vector>
#if __cplusplus >= 202302L
#include <expected>
#endif

int main() {
    std::vector<int> v(100);
    std::iota(v.begin(), v.end(), 1);              // 1..100
    long sum = std::accumulate(v.begin(), v.end(), 0L);  // 5050

#if __cplusplus >= 202302L
    std::expected<int, std::string> answer = 42;   // C++23 library feature
    std::cout << "GCCHELLO cxxstd=" << __cplusplus
              << " sum=" << sum
              << " expected=" << answer.value() << "\n";
#else
    std::cout << "GCCHELLO cxxstd=" << __cplusplus
              << " sum=" << sum << "\n";
#endif
    return 0;
}
