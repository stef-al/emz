/* emcm_permsg.c -- per-message ("random access") experiment with a primed
 * context-mixing model.
 *
 *   emcm_permsg <prime-file> <messages.corpus> [tbits [sizes-out]]
 *
 * The model is first trained on <prime-file> (e.g. concatenated training
 * messages); that state is then frozen.  Every message of <messages.corpus>
 * (canonical corpus format: path\n length\n bytes) is compressed on its own,
 * starting from the frozen state (a fork() per message gives each one a
 * private copy-on-write copy of the model), exactly as a decoder holding the
 * same primed model would decode it.  Prints total input / output bytes and,
 * if sizes-out is given, writes one compressed size per message to that file.
 * The per-message output includes the 4-byte arithmetic-coder flush; a real
 * container would add a length field.
 *
 * Build: cc -O3 -ffp-contract=off -o emcm_permsg scripts/emcm_permsg.c -lm
 *        (compile from the repository root so that "emcm.c" is found, or add -I.)
 */
#define main emcm_main
#include "../emcm.c"
#undef main
#include <unistd.h>
#include <sys/wait.h>

static U8 *slurp(const char *fn, size_t *n) { return read_file(fn, n); }

int main(int argc, char **argv) {
  if (argc < 3) { fprintf(stderr, "usage: emcm_permsg prime.bin msgs.corpus [tbits]\n"); return 1; }
  init_stretch(); init_states();
  for (int i = 0; i < 1024; i++) dt[i] = 16384 / (i + i + 3);
  size_t np, nm; U8 *prime = slurp(argv[1], &np); U8 *msgs = slurp(argv[2], &nm);
  int tbits = argc > 3 ? atoi(argv[3]) : 22;
  FILE *sizes = argc > 4 ? fopen(argv[4], "w") : NULL;
  /* index messages */
  size_t cap = 1024, cnt = 0, maxlen = 0; size_t *off = malloc(cap * sizeof *off), *len = malloc(cap * sizeof *len);
  for (size_t p = 0; p < nm; ) {
    size_t q = p; while (q < nm && msgs[q] != '\n') q++;            /* path */
    size_t r = q + 1, L = 0; while (r < nm && msgs[r] != '\n') L = L * 10 + (msgs[r++] - '0');
    if (cnt == cap) { cap *= 2; off = realloc(off, cap * sizeof *off); len = realloc(len, cap * sizeof *len); }
    off[cnt] = r + 1; len[cnt] = L; cnt++; if (L > maxlen) maxlen = L;
    p = r + 1 + L;
  }
  int mbits = ilog2u(np + maxlen + 1); if (mbits < 16) mbits = 16; if (mbits > 24) mbits = 24;
  U8 *hist = calloc(np + maxlen + 16, 1);
  memcpy(hist, prime, np);
  Pred *P = malloc(sizeof(Pred)); pred_init(P, (U32)(np + maxlen + 8), tbits, mbits, hist);
  for (size_t i = 0; i < np; i++) {                       /* priming: model updates, no coding */
    int c = prime[i];
    for (int j = 7; j >= 0; j--) { pred_p(P); pred_update(P, (c >> j) & 1); }
  }
  U64 tin = 0, tout = 0;
  int fd[2];
  for (size_t m = 0; m < cnt; m++) {
    if (pipe(fd)) { perror("pipe"); return 1; }
    pid_t pid = fork();
    if (pid == 0) {
      close(fd[0]);
      AC a = {0, 0xffffffffu, 0, NULL, 0, 0};
      const U8 *s = msgs + off[m];
      for (size_t i = 0; i < len[m]; i++)
        for (int j = 7; j >= 0; j--) { int y = (s[i] >> j) & 1; ac_encode(&a, y, pred_p(P)); pred_update(P, y); }
      for (int i = 0; i < 4; i++) { ac_put(&a, (U8)(a.x1 >> 24)); a.x1 <<= 8; }
      U64 out = a.pos;
      if (write(fd[1], &out, sizeof out) != sizeof out) _exit(1);
      _exit(0);
    }
    close(fd[1]);
    U64 out = 0;
    if (read(fd[0], &out, sizeof out) != sizeof out) { fprintf(stderr, "child failed\n"); return 1; }
    close(fd[0]); waitpid(pid, NULL, 0);
    tin += len[m]; tout += out;
    if (sizes) fprintf(sizes, "%llu\n", (unsigned long long)out);
  }
  if (sizes) fclose(sizes);
  printf("{\"messages\": %zu, \"prime_bytes\": %zu, \"tbits\": %d, \"in\": %llu, \"out\": %llu, \"ratio\": %.3f}\n",
         cnt, np, tbits, (unsigned long long)tin, (unsigned long long)tout, (double)tin / tout);
  return 0;
}
