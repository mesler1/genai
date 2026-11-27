# C: house rules and the gotchas that bite

- Compile with warnings on and treat them as errors while developing: `-Wall -Wextra -Werror -O2 -g`. For tests, also `-fsanitize=address,undefined`.
- Check every return value that can fail: `malloc`, `fopen`, `read`, `write`, `snprintf`. Report errors with `perror` or `strerror(errno)` to stderr, and exit non-zero.
- Strings: no `gets`, `strcpy`, `strcat` or `sprintf` into fixed buffers. Use `snprintf` and check its result against the buffer size (truncation returns the length it wanted). `strncpy` doesn't always add the terminating zero.
- Sizes are `size_t`; compare and loop with it, and watch for overflow in `n * size` before `malloc` (use `calloc` or check).
- Every `malloc` has one owner that frees it. Set pointers to `NULL` after `free` when they live on.
- Integer gotchas: signed overflow is undefined; `char` may be signed; shifting by the type's width or more is undefined; `abs(INT_MIN)` overflows.
- Pass `const` for inputs that aren't changed. Keep functions short and file-local ones `static`.
- Build with a Makefile (or the project's build system) rather than long command lines; on a cluster, load the compiler module first and name it in the build notes.
