/* emcm.c -- bitwise context-mixing coder (paq/lpaq family) used as the
 * entropy back end of the structure-aware e-mail compressor.
 *
 *   emcm c <in> <out>     compress one stream
 *   emcm d <in> <out>     decompress
 *
 * Environment (all optional):
 *   EMCM_TBMAX=n   cap on hash-table size, 2^n 16-byte buckets (default 26;
 *                  emz uses 27 for the body stream).  Stored in the stream
 *                  header, so the decoder needs no setting.
 *   EMCM_STATS=1   print coding cost by match-length class (compression only)
 * Experimental model knobs used during development -- they change the model
 * and are NOT stored in the header, so they must be identical for compression
 * and decompression (emz uses the defaults): EMCM_LR, EMCM_LRF, EMCM_CTX,
 * EMCM_DISC, EMCM_TB.
 *
 * Model: nibble-bucketed hashed contexts (orders 1-4,6,8, word unigram and
 * bigram, column/"byte above"), bit-history state machines mapped to
 * probabilities by adaptive StateMaps, two match models (short and long
 * minimum length) over the whole stream history, three gated logistic mixers
 * combined by a final mixer, and three APM/SSE refinement stages.
 */
/* The model uses float arithmetic in its mixers.  Archives are portable only
 * if every build rounds identically, so fused multiply-add contraction must be
 * disabled (-ffp-contract=off, see Makefile; the pragma covers clang). */
#if defined(__clang__)
#pragma STDC FP_CONTRACT OFF
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>

typedef uint8_t U8; typedef uint16_t U16; typedef uint32_t U32; typedef uint64_t U64;

/* ---------- logistic helpers ---------- */
static int squash(int d) {
  static const int t[33] = {1,2,3,6,10,16,27,45,73,120,194,310,488,747,1101,1546,
    2047,2549,2994,3348,3607,3785,3901,3975,4024,4050,4068,4079,4085,4089,4092,4093,4094};
  if (d > 2047) return 4095;
  if (d < -2047) return 1;
  int w = d & 127;
  d = (d >> 7) + 16;
  return (t[d] * (128 - w) + t[d + 1] * w + 64) >> 7;
}
static short stretch_t[4096];
static void init_stretch(void) {
  int pi = 0;
  for (int x = -2047; x <= 2047; x++) {
    int v = squash(x);
    for (int i = pi; i <= v; i++) stretch_t[i] = (short)x;
    pi = v + 1;
  }
  for (int i = pi; i < 4096; i++) stretch_t[i] = 2047;
}
#define stretch(p) (stretch_t[(p)])

static inline U32 hash2(U32 a, U32 b) {
  U64 h = (U64)a * 0x9E3779B97F4A7C15ull ^ ((U64)(b + 0x632BE5AB) * 0xC2B2AE3D27D4EB4Full);
  h ^= h >> 29; h *= 0xBF58476D1CE4E5B9ull; h ^= h >> 32;
  return (U32)h;
}

/* ---------- bit-history state machine ----------
 * State = (n0, n1) counts; on bit y the y-count grows (capped) and the other
 * count, if > 2, is roughly halved (favours recent history).  States are
 * numbered in order of total count so the number doubles as a priority. */
static U8 st_next[256][2];
static int st_n0[256], st_n1[256], nstates;
static int disc_mode = 0;
static int disc(int b) {
  switch (disc_mode) {
    case 1: return b > 3 ? (b * 2 + 2) / 3 : b;   /* gentler */
    case 2: return b > 1 ? (b + 1) / 2 : b;       /* harsher */
    case 3: return b > 4 ? b / 2 + 2 : b;
    default: return b > 2 ? (b + 1) / 2 : b;
  }
}
static void st_step(int *a, int *b, int y, int L) {
  if (!y) { *a = *a + 1 > L ? L : *a + 1; *b = disc(*b); }
  else    { *b = *b + 1 > L ? L : *b + 1; *a = disc(*a); }
}
static void init_states(void) {
  int L = 63, id[64][64];
  for (;;) {
    static U8 seen[64][64]; memset(seen, 0, sizeof seen);
    int qa[4096], qb[4096], qn = 0, cnt = 0;
    seen[0][0] = 1; qa[qn] = 0; qb[qn++] = 0;
    for (int q = 0; q < qn; q++)
      for (int y = 0; y < 2; y++) {
        int a = qa[q], b = qb[q]; st_step(&a, &b, y, L);
        if (!seen[a][b]) { seen[a][b] = 1; qa[qn] = a; qb[qn++] = b; }
      }
    cnt = qn;
    if (cnt <= 256) {
      /* sort by total count, then n1 */
      for (int i = 0; i < qn; i++)
        for (int j = i + 1; j < qn; j++) {
          int ti = qa[i] + qb[i], tj = qa[j] + qb[j];
          if (tj < ti || (tj == ti && qb[j] < qb[i])) {
            int t = qa[i]; qa[i] = qa[j]; qa[j] = t; t = qb[i]; qb[i] = qb[j]; qb[j] = t;
          }
        }
      for (int i = 0; i < qn; i++) { id[qa[i]][qb[i]] = i; st_n0[i] = qa[i]; st_n1[i] = qb[i]; }
      for (int i = 0; i < qn; i++)
        for (int y = 0; y < 2; y++) {
          int a = qa[i], b = qb[i]; st_step(&a, &b, y, L);
          st_next[i][y] = (U8)id[a][b];
        }
      nstates = qn;
      return;
    }
    L--;
  }
}

/* ---------- StateMap: context -> adaptive probability ---------- */
static int dt[1024];
typedef struct { U32 *t; U32 cx; } StateMap;
static void sm_init(StateMap *s, int n, int bit_history) {
  s->t = (U32 *)malloc(sizeof(U32) * n); s->cx = 0;
  for (int i = 0; i < n; i++) {
    U32 p = 1u << 31;
    if (bit_history) {
      int k = i % 256; if (k >= nstates) k = 0;
      double q = (st_n1[k] + 0.4) / (st_n0[k] + st_n1[k] + 0.8);
      p = (U32)(q * 4294967295.0);
    }
    s->t[i] = p & 0xfffffc00u;
  }
}
static inline int sm_p(StateMap *s, U32 cx) { s->cx = cx; return s->t[cx] >> 20; }
static inline void sm_update(StateMap *s, int y, int limit) {
  U32 *e = &s->t[s->cx];
  int n = *e & 1023; int p = *e >> 10;
  if (n < limit) ++*e; else *e = (*e & 0xfffffc00u) | (U32)limit;
  int64_t d = (int64_t)(((y << 22) - p) >> 3) * dt[n];
  *e += (U32)d & 0xfffffc00u;
}

/* ---------- APM / SSE ---------- */
typedef struct { U16 *t; int idx; } APM;
static void apm_init(APM *a, int n) {
  a->t = (U16 *)malloc(sizeof(U16) * n * 33); a->idx = 0;
  for (int i = 0; i < n; i++)
    for (int j = 0; j < 33; j++) a->t[i * 33 + j] = (U16)(squash((j - 16) * 128) * 16);
}
static inline int apm_p(APM *a, int pr, U32 cx) {
  int s = (stretch(pr) + 2048) * 32;
  int wt = s & 0xfff;
  U32 base = cx * 33 + (s >> 12);
  a->idx = base + (wt >> 11);
  return (a->t[base] * (4096 - wt) + a->t[base + 1] * wt) >> 16;
}
static inline void apm_update(APM *a, int y, int rate) {
  int g = (y << 16) + (y << rate) - y - y;
  a->t[a->idx] += (g - a->t[a->idx]) >> rate;
}

/* ---------- Mixer (float, gated by a selector context) ---------- */
typedef struct { int n, nsel; float *w; float *x; int sel; float lr; int pr; } Mixer;
static void mx_init(Mixer *m, int n, int nsel, float lr, float w0) {
  m->n = n; m->nsel = nsel; m->lr = lr; m->sel = 0; m->pr = 2048;
  m->w = (float *)malloc(sizeof(float) * n * nsel);
  m->x = (float *)calloc(n, sizeof(float));
  for (int i = 0; i < n * nsel; i++) m->w[i] = w0;
}
static inline int mx_p(Mixer *m, int sel) {
  m->sel = sel;
  const float *w = m->w + (size_t)sel * m->n;
  float dot = 0;
  for (int i = 0; i < m->n; i++) dot += w[i] * m->x[i];
  int d = (int)(dot * 256.0f);
  if (d > 2047) d = 2047; if (d < -2047) d = -2047;
  return m->pr = squash(d);
}
static inline void mx_update(Mixer *m, int y) {
  float err = ((y << 12) - m->pr) * (1.0f / 4096.0f) * m->lr;
  float *w = m->w + (size_t)m->sel * m->n;
  for (int i = 0; i < m->n; i++) w[i] += err * m->x[i];
}

/* ---------- bucketed hash table: 16-byte buckets (check + 15 node states) */
typedef struct { U8 *t; U32 nb; } HashTable;   /* nb = number of buckets (2^k) */
static void ht_init(HashTable *h, int bits) {
  h->nb = 1u << bits;
  if (posix_memalign((void **)&h->t, 64, (size_t)h->nb * 16)) { fprintf(stderr, "oom\n"); exit(1); }
  memset(h->t, 0, (size_t)h->nb * 16);
}
static inline U8 *ht_get(HashTable *h, U32 i) {
  U8 chk = (U8)((i >> 24) ^ (i >> 8) ^ 0x5a); if (!chk) chk = 1;
  U32 b = (i & (h->nb - 1)) * 16;
  U8 *t = h->t;
  if (t[b] == chk) return t + b;
  if (t[b ^ 16] == chk) return t + (b ^ 16);
  if (t[b ^ 32] == chk) return t + (b ^ 32);
  U32 r = b;
  if (t[r + 1] > t[(b ^ 16) + 1]) r = b ^ 16;
  if (t[r + 1] > t[(b ^ 32) + 1]) r = b ^ 32;
  memset(t + r, 0, 16); t[r] = chk;
  return t + r;
}

/* ---------- match model ---------- */
typedef struct {
  U32 *ht; U32 hmask; int minlen, shift; U32 h, ptr, len; StateMap sm; int eb; /* expected bit or -1 */
} Match;
#define MAXLEN 65000
static void mm_init(Match *m, int bits, int minlen) {
  m->ht = (U32 *)calloc((size_t)1 << bits, sizeof(U32)); m->hmask = (1u << bits) - 1;
  m->minlen = minlen; m->shift = (bits + minlen - 1) / minlen; if (m->shift < 1) m->shift = 1;
  m->h = 0; m->ptr = 0; m->len = 0; m->eb = -1;
  sm_init(&m->sm, 64 * 2, 0);
}
static inline int lenbucket(U32 len) { return len < 16 ? (int)len : (len < 32 ? 16 + ((len - 16) >> 2) : (len < 64 ? 20 + ((len - 32) >> 3) : (len < 512 ? 24 + ((len - 64) >> 6) : 31))); }

/* ---------- predictor ---------- */
#define NCTX 16
static int nctx = 9; static int ctxsel[NCTX] = {0,1,2,3,4,5,6,7,8};
typedef struct {
  U8 *buf; U32 pos, cap;          /* history */
  HashTable T;
  StateMap sm[NCTX];
  U8 *cp[NCTX];                   /* current node pointers */
  U8 *bucket[NCTX];
  U32 hctx[NCTX];
  Match mm[2];
  StateMap mmsm2;                 /* combined match agreement */
  Mixer m1, m2, m3, mf;
  APM a1, a2, a3;
  U32 c0, c4, c8, c12, bitpos, nib;    /* partial byte with leading 1, last bytes */
  U32 word0, word1, word2, col, linestart, prevlinestart;
  int pr, pmix;
} Pred;

static void pred_init(Pred *P, U32 cap, int tbits, int mbits, U8 *ext) {
  memset(P, 0, sizeof *P);
  P->cap = cap + 8; P->buf = ext ? ext : (U8 *)calloc(P->cap, 1);
  ht_init(&P->T, tbits);
  for (int i = 0; i < nctx; i++) sm_init(&P->sm[i], 256, 1);
  mm_init(&P->mm[0], mbits, 6);
  mm_init(&P->mm[1], mbits, 24);
  sm_init(&P->mmsm2, 4096, 0);
  int nin = 2 * nctx + 3 + 1;
  float lr = getenv("EMCM_LR") ? (float)atof(getenv("EMCM_LR")) : 0.0035f;
  float lrf = getenv("EMCM_LRF") ? (float)atof(getenv("EMCM_LRF")) : 0.0015f;
  mx_init(&P->m1, nin, 256, lr, 0.25f);
  mx_init(&P->m2, nin, 320, lr, 0.25f);
  mx_init(&P->m3, nin, 256 * 8, lr, 0.25f);
  mx_init(&P->mf, 4, 256, lrf, 0.33f);
  apm_init(&P->a1, 256);
  apm_init(&P->a2, 65536);
  apm_init(&P->a3, 65536);
  P->c0 = 1; P->pr = 2048;
  for (int i = 0; i < nctx; i++) { P->bucket[i] = ht_get(&P->T, hash2(i, 0)); P->cp[i] = P->bucket[i] + 1; }
}

static inline int mm_predict(Pred *P, Match *m) {
  /* expected bit from match pointer if bits so far agree */
  if (m->len > 0) {
    U32 pb = P->buf[m->ptr] | 0x100;
    if ((pb >> (8 - P->bitpos)) == P->c0) { m->eb = (pb >> (7 - P->bitpos)) & 1; }
    else { m->len = 0; m->eb = -1; }
  } else m->eb = -1;
  int cx = m->eb < 0 ? 0 : (lenbucket(m->len) * 2 + m->eb) * 2 + 0;
  return stretch(sm_p(&m->sm, cx));
}

static void mm_byte(Pred *P, Match *m) {
  U32 c = P->buf[P->pos - 1];
  m->h = (m->h * (1u << m->shift) + c + 1) & m->hmask;
  if (m->len > 0 && P->buf[m->ptr] == c) { m->len++; m->ptr++; if (m->len > MAXLEN) m->len = MAXLEN; }
  else m->len = 0;
  if (m->len == 0 && P->pos >= (U32)m->minlen) {
    U32 p = m->ht[m->h];
    if (p > 0 && p < P->pos) {
      U32 l = 0;
      while (l < MAXLEN && l < p && P->buf[p - l - 1] == P->buf[P->pos - l - 1]) l++;
      if (l >= (U32)m->minlen) { m->len = l; m->ptr = p; }
    }
  }
  m->ht[m->h] = P->pos;
}

static int pred_p(Pred *P) {
  float *x1 = P->m1.x;
  int k = 0, nseen = 0;
  for (int i = 0; i < nctx; i++) {
    int s = *P->cp[i];
    if (s) nseen++;
    int pp = sm_p(&P->sm[i], s);
    x1[k++] = stretch(pp) * (1.0f / 256.0f);
    x1[k++] = s ? (pp - 2048) * (1.0f / 1024.0f) : 0.0f;
  }
  int s0 = mm_predict(P, &P->mm[0]);
  int s1 = mm_predict(P, &P->mm[1]);
  x1[k++] = s0 * (1.0f / 256.0f);
  x1[k++] = s1 * (1.0f / 256.0f);
  /* agreement of the two match models */
  int e0 = P->mm[0].eb, e1 = P->mm[1].eb;
  U32 acx = (U32)((e0 + 1) * 3 + (e1 + 1)) * 32 + (U32)(lenbucket(P->mm[0].len > P->mm[1].len ? P->mm[0].len : P->mm[1].len));
  x1[k++] = stretch(sm_p(&P->mmsm2, acx * 8 + P->bitpos)) * (1.0f / 256.0f);
  x1[k++] = 0.5f; /* bias */
  memcpy(P->m2.x, x1, sizeof(float) * k);
  memcpy(P->m3.x, x1, sizeof(float) * k);
  int lb = lenbucket(P->mm[0].len); if (lb > 15) lb = 15;
  int p1 = mx_p(&P->m1, P->c0);
  int p2 = mx_p(&P->m2, (nseen * 16 + lb) * 2 + (P->mm[1].len > 0));
  int p3 = mx_p(&P->m3, (P->c4 & 0xff) * 8 + P->bitpos);
  P->mf.x[0] = stretch(p1) / 256.0f; P->mf.x[1] = stretch(p2) / 256.0f; P->mf.x[2] = stretch(p3) / 256.0f; P->mf.x[3] = 0.5f;
  int pm = mx_p(&P->mf, P->c0);
  P->pmix = pm;
  int q1 = apm_p(&P->a1, pm, P->c0);
  int q2 = apm_p(&P->a2, pm, (P->c0 | ((P->c4 & 0xff) << 8)) & 0xffff);
  U32 mctx = P->mm[0].eb < 0 ? P->c0 : (P->c0 | ((U32)(P->buf[P->mm[0].ptr]) << 8)) ^ ((U32)(lb > 7) << 15);
  int q3 = apm_p(&P->a3, pm, hash2(mctx, P->mm[0].eb < 0) & 0xffff);
  int p = (2 * pm + q1 + 2 * q2 + 3 * q3 + 4) >> 3;
  if (p < 1) p = 1; if (p > 4095) p = 4095;
  return P->pr = p;
}

static void pred_update(Pred *P, int y) {
  for (int i = 0; i < nctx; i++) {
    *P->cp[i] = st_next[*P->cp[i]][y];
    sm_update(&P->sm[i], y, 1023);
  }
  sm_update(&P->mm[0].sm, y, 1023);
  sm_update(&P->mm[1].sm, y, 1023);
  sm_update(&P->mmsm2, y, 1023);
  mx_update(&P->m1, y); mx_update(&P->m2, y); mx_update(&P->m3, y); mx_update(&P->mf, y);
  apm_update(&P->a1, y, 7); apm_update(&P->a2, y, 7); apm_update(&P->a3, y, 7);

  P->c0 = (P->c0 << 1) | y; P->bitpos++;
  if (P->bitpos == 8) {
    U32 c = P->c0 & 0xff;
    P->buf[P->pos++] = (U8)c;
    P->c12 = (P->c12 << 8) | (P->c8 >> 24);
    P->c8 = (P->c8 << 8) | (P->c4 >> 24);
    P->c4 = (P->c4 << 8) | c;
    P->c0 = 1; P->bitpos = 0;
    /* word / line state */
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c >= 128) {
      U32 lc = (c >= 'A' && c <= 'Z') ? c + 32 : c;
      P->word0 = hash2(P->word0 + 0x1234, lc);
    } else if (P->word0) { P->word2 = P->word1; P->word1 = P->word0; P->word0 = 0; }
    if (c == '\n') { P->prevlinestart = P->linestart; P->linestart = P->pos; P->col = 0; }
    else P->col++;
    U32 above = 0;
    if (P->prevlinestart + P->col < P->linestart) above = P->buf[P->prevlinestart + P->col];
    U32 c4 = P->c4, c8 = P->c8;
    U32 H[NCTX];
    H[0] = hash2(1, c4 & 0xff);
    H[1] = hash2(2, c4 & 0xffff);
    H[2] = hash2(3, c4 & 0xffffff);
    H[3] = hash2(4, c4);
    H[4] = hash2(hash2(6, c4), c8 & 0xffff);
    H[5] = hash2(hash2(8, c4), c8);
    H[6] = hash2(hash2(9, P->word0), P->word0 ? 0 : (c4 & 0xff));
    H[7] = hash2(hash2(10, P->word0), P->word1);
    H[8] = hash2(hash2(11, above), (P->col < 48 ? P->col : 48) | ((c4 & 0xff) << 8));
    H[9] = hash2(hash2(5, c4), c8 & 0xff);
    H[10] = hash2(hash2(hash2(12, P->word0), P->word1), P->word2);
    H[11] = hash2(hash2(hash2(13, c4), c8), P->c12);
    H[12] = hash2(hash2(14, c4 & 0xffff00), 0);            /* sparse: bytes 2-3 */
    H[13] = hash2(hash2(15, P->word0), c4 & 0xff);          /* word + last byte */
    for (int i = 0; i < nctx; i++) P->hctx[i] = H[ctxsel[i]];
    mm_byte(P, &P->mm[0]);
    mm_byte(P, &P->mm[1]);
    for (int i = 0; i < nctx; i++) { P->bucket[i] = ht_get(&P->T, P->hctx[i]); P->cp[i] = P->bucket[i] + 1; }
  } else if (P->bitpos == 4) {
    for (int i = 0; i < nctx; i++) { P->bucket[i] = ht_get(&P->T, hash2(P->hctx[i], P->c0)); P->cp[i] = P->bucket[i] + 1; }
  } else {
    /* node index within nibble: 1, 2-3, 4-7, 8-15 */
    U32 j = (P->bitpos < 4) ? (P->c0 & ((1u << P->bitpos) - 1)) | (1u << P->bitpos)
                            : ((P->c0 & ((1u << (P->bitpos - 4)) - 1)) | (1u << (P->bitpos - 4)));
    for (int i = 0; i < nctx; i++) P->cp[i] = P->bucket[i] + j;
  }
}

/* ---------- arithmetic coder ---------- */
typedef struct { U32 x1, x2, x; U8 *buf; size_t pos, cap; } AC;
static inline void ac_put(AC *a, U8 c) {
  if (a->pos == a->cap) { a->cap = a->cap * 2 + 1024; a->buf = (U8 *)realloc(a->buf, a->cap); }
  a->buf[a->pos++] = c;
}
static inline void ac_encode(AC *a, int y, int p) {
  U32 xmid = a->x1 + (U32)(((U64)(a->x2 - a->x1) * (U32)p) >> 12);
  if (y) a->x2 = xmid; else a->x1 = xmid + 1;
  while (((a->x1 ^ a->x2) & 0xff000000u) == 0) { ac_put(a, (U8)(a->x2 >> 24)); a->x1 <<= 8; a->x2 = (a->x2 << 8) | 255; }
}
static inline int ac_decode(AC *a, int p) {
  U32 xmid = a->x1 + (U32)(((U64)(a->x2 - a->x1) * (U32)p) >> 12);
  int y = a->x <= xmid;
  if (y) a->x2 = xmid; else a->x1 = xmid + 1;
  while (((a->x1 ^ a->x2) & 0xff000000u) == 0) {
    a->x1 <<= 8; a->x2 = (a->x2 << 8) | 255;
    a->x = (a->x << 8) | (a->pos < a->cap ? a->buf[a->pos++] : 0);
  }
  return y;
}

static int ilog2u(U64 n) { int b = 0; while ((1ull << b) < n) b++; return b; }

static U8 *read_file(const char *fn, size_t *n) {
  FILE *f = fopen(fn, "rb"); if (!f) { perror(fn); exit(1); }
  fseek(f, 0, SEEK_END); *n = (size_t)ftell(f); fseek(f, 0, SEEK_SET);
  U8 *b = (U8 *)malloc(*n + 1);
  if (*n && fread(b, 1, *n, f) != *n) { perror("read"); exit(1); }
  fclose(f); return b;
}

int main(int argc, char **argv) {
  if (argc < 4) { fprintf(stderr, "usage: emcm c|d in out\n"); return 1; }
  if (getenv("EMCM_DISC")) disc_mode = atoi(getenv("EMCM_DISC"));
  init_stretch(); init_states();
  if (getenv("EMCM_VERBOSE")) fprintf(stderr, "states %d\n", nstates);
  if (getenv("EMCM_CTX")) { nctx = 0; for (char *q = getenv("EMCM_CTX"); *q; ) { ctxsel[nctx++] = (int)strtol(q, &q, 10); if (*q == ',') q++; } }
  for (int i = 0; i < 1024; i++) dt[i] = 16384 / (i + i + 3);
  size_t n; U8 *in = read_file(argv[2], &n);
  FILE *fo = fopen(argv[3], "wb"); if (!fo) { perror(argv[3]); return 1; }
  if (argv[1][0] == 'c') {
    int tbits = ilog2u(n + 1) + (getenv("EMCM_TB") ? atoi(getenv("EMCM_TB")) : -2); int tbmax = getenv("EMCM_TBMAX") ? atoi(getenv("EMCM_TBMAX")) : 26;
    if (tbits < 12) tbits = 12; if (tbits > tbmax) tbits = tbmax;
    int mbits = ilog2u(n + 1); if (mbits < 12) mbits = 12; if (mbits > 24) mbits = 24;
    U8 hdr[14] = {'E', 'M', 'C', '1'};
    for (int i = 0; i < 8; i++) hdr[4 + i] = (U8)((U64)n >> (8 * i));
    hdr[12] = (U8)tbits; hdr[13] = (U8)mbits;
    fwrite(hdr, 1, 14, fo);
    if (n) {
      Pred *P = (Pred *)malloc(sizeof(Pred)); pred_init(P, (U32)n, tbits, mbits, in);  /* history = input buffer */
      AC a = {0, 0xffffffffu, 0, NULL, 0, 0};
      int stats = getenv("EMCM_STATS") != NULL;
      double cost[4] = {0}; U64 cnt[4] = {0};
      FILE *costf = getenv("EMCM_COSTFILE") ? fopen(getenv("EMCM_COSTFILE"), "wb") : NULL; double bytecost = 0;
      for (size_t i = 0; i < n; i++) {
        int c = in[i];
        U32 ml = P->mm[0].len > P->mm[1].len ? P->mm[0].len : P->mm[1].len;
        int cat = ml == 0 ? 0 : (ml < 16 ? 1 : (ml < 64 ? 2 : 3));
        for (int j = 7; j >= 0; j--) {
          int y = (c >> j) & 1;
          int p = pred_p(P);
          if (stats) { double cb = -log2((y ? p : 4096 - p) / 4096.0); cost[cat] += cb; if (costf) bytecost += cb; }
          ac_encode(&a, y, p);
          pred_update(P, y);
        }
        cnt[cat]++;
        if (costf) { unsigned short q = (unsigned short)(bytecost * 256 > 65535 ? 65535 : bytecost * 256); fwrite(&q, 2, 1, costf); bytecost = 0; }
      }
      if (costf) fclose(costf);
      if (stats) for (int k = 0; k < 4; k++) fprintf(stderr, "matchlen cat %d: bytes %llu  cost %.0f bytes  (%.3f bpc)\n", k, (unsigned long long)cnt[k], cost[k] / 8, cnt[k] ? cost[k] / cnt[k] : 0);
      for (int i = 0; i < 4; i++) { ac_put(&a, (U8)(a.x1 >> 24)); a.x1 <<= 8; }
      fwrite(a.buf, 1, a.pos, fo);
    }
  } else {
    if (n < 14 || memcmp(in, "EMC1", 4)) { fprintf(stderr, "bad stream\n"); return 1; }
    U64 len = 0; for (int i = 0; i < 8; i++) len |= (U64)in[4 + i] << (8 * i);
    int tbits = in[12], mbits = in[13];
    if (len) {
      U8 *out = (U8 *)calloc(len + 8, 1);
      Pred *P = (Pred *)malloc(sizeof(Pred)); pred_init(P, (U32)len, tbits, mbits, out);  /* history = output buffer */
      AC a = {0, 0xffffffffu, 0, in + 14, 0, n - 14};
      for (int i = 0; i < 4; i++) a.x = (a.x << 8) | (a.pos < a.cap ? a.buf[a.pos++] : 0);
      for (U64 i = 0; i < len; i++) {
        int c = 0;
        for (int j = 0; j < 8; j++) { int y = ac_decode(&a, pred_p(P)); pred_update(P, y); c = (c << 1) | y; }
        if (out[i] != (U8)c) { fprintf(stderr, "history mismatch\n"); return 1; }
      }
      fwrite(out, 1, len, fo);
    }
  }
  fclose(fo);
  return 0;
}
