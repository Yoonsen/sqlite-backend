// postings.c
#include <sqlite3ext.h>

// If SQLite headers disable loadable extensions, restore the API hookups
#ifdef SQLITE_OMIT_LOAD_EXTENSION
#undef SQLITE_EXTENSION_INIT1
#undef SQLITE_EXTENSION_INIT2
#undef SQLITE_EXTENSION_INIT3
#define SQLITE_EXTENSION_INIT1     const sqlite3_api_routines *sqlite3_api=0;
#define SQLITE_EXTENSION_INIT2(v)  sqlite3_api=v;
#define SQLITE_EXTENSION_INIT3     extern const sqlite3_api_routines *sqlite3_api;
#endif

#ifndef sqlite3_create_function
#define sqlite3_create_function   sqlite3_api->create_function
#define sqlite3_result_error      sqlite3_api->result_error
#define sqlite3_result_int        sqlite3_api->result_int
#define sqlite3_result_int64      sqlite3_api->result_int64
#define sqlite3_result_null       sqlite3_api->result_null
#define sqlite3_result_blob       sqlite3_api->result_blob
#define sqlite3_result_text       sqlite3_api->result_text
#define sqlite3_value_blob        sqlite3_api->value_blob
#define sqlite3_value_bytes       sqlite3_api->value_bytes
#define sqlite3_value_int         sqlite3_api->value_int
#define sqlite3_value_int64       sqlite3_api->value_int64
#define sqlite3_realloc           sqlite3_api->realloc
#define sqlite3_free              sqlite3_api->free
#define sqlite3_aggregate_context sqlite3_api->aggregate_context
#endif

SQLITE_EXTENSION_INIT1

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(__GNUC__)
#define LIKELY(x) __builtin_expect(!!(x), 1)
#define UNLIKELY(x) __builtin_expect(!!(x), 0)
#else
#define LIKELY(x) (x)
#define UNLIKELY(x) (x)
#endif

// Enkle varint/delta helpers – fyll ut senere
static inline uint64_t read_varint(const uint8_t **p, const uint8_t *end) {
    const uint8_t *ptr = *p;
    if (UNLIKELY(ptr >= end)) return 0;

    uint8_t b0 = *ptr++;
    if (LIKELY((b0 & 0x80) == 0)) {
        *p = ptr;
        return (uint64_t)b0;
    }

    if (UNLIKELY(ptr >= end)) {
        *p = ptr;
        return (uint64_t)(b0 & 0x7f);
    }
    uint8_t b1 = *ptr++;
    uint64_t x = (uint64_t)(b0 & 0x7f) | ((uint64_t)(b1 & 0x7f) << 7);
    if (LIKELY((b1 & 0x80) == 0)) {
        *p = ptr;
        return x;
    }

    if (UNLIKELY(ptr >= end)) {
        *p = ptr;
        return x;
    }
    uint8_t b2 = *ptr++;
    x |= ((uint64_t)(b2 & 0x7f) << 14);
    if (LIKELY((b2 & 0x80) == 0)) {
        *p = ptr;
        return x;
    }

    int shift = 21;
    while (ptr < end) {
        uint8_t b = *ptr++;
        x |= (uint64_t)(b & 0x7f) << shift;
        if ((b & 0x80) == 0) break;
        shift += 7;
    }
    *p = ptr;
    return x;
}

// Decode postings BLOB to next seq (running total)
static int next_seq(const uint8_t **p, const uint8_t *end, uint64_t *acc) {
    if (*p >= end) return 0;
    uint64_t delta = read_varint(p, end);
    *acc += delta;
    return 1;
}

// Minimal JSON builder for integer arrays
static int json_append_char(char **buf, int *len, int *cap, char c) {
    if (*len + 1 >= *cap) {
        int new_cap = (*cap == 0) ? 128 : (*cap * 2);
        char *new_buf = sqlite3_realloc(*buf, new_cap);
        if (!new_buf) return 0;
        *buf = new_buf;
        *cap = new_cap;
    }
    (*buf)[(*len)++] = c;
    (*buf)[*len] = '\0';
    return 1;
}

static int json_append_int64(char **buf, int *len, int *cap, sqlite3_int64 v) {
    char tmp[32];
    int n = snprintf(tmp, sizeof(tmp), "%lld", (long long)v);
    if (n < 0) return 0;
    for (int i = 0; i < n; i++) {
        if (!json_append_char(buf, len, cap, tmp[i])) return 0;
    }
    return 1;
}

static int append_varint_bytes(uint64_t v, unsigned char **buf, int *len, int *cap) {
    while (1) {
        unsigned char byte = (unsigned char)(v & 0x7f);
        v >>= 7;
        if (v) byte |= 0x80;
        if (*len + 1 >= *cap) {
            int new_cap = (*cap == 0) ? 128 : (*cap * 2);
            unsigned char *new_buf = sqlite3_realloc(*buf, new_cap);
            if (!new_buf) return 0;
            *buf = new_buf;
            *cap = new_cap;
        }
        (*buf)[(*len)++] = byte;
        if (!v) break;
    }
    return 1;
}

static int union_blobs(
    const unsigned char *a, int a_len,
    const unsigned char *b, int b_len,
    unsigned char **out, int *out_len
) {
    *out = NULL;
    *out_len = 0;
    if ((!a || a_len <= 0) && (!b || b_len <= 0)) {
        return 1;
    }
    if (!a || a_len <= 0) {
        unsigned char *buf = sqlite3_malloc(b_len);
        if (!buf) return 0;
        memcpy(buf, b, b_len);
        *out = buf;
        *out_len = b_len;
        return 1;
    }
    if (!b || b_len <= 0) {
        unsigned char *buf = sqlite3_malloc(a_len);
        if (!buf) return 0;
        memcpy(buf, a, a_len);
        *out = buf;
        *out_len = a_len;
        return 1;
    }

    const uint8_t *pa = a, *pb = b;
    const uint8_t *ea = a + a_len, *eb = b + b_len;
    uint64_t acc_a = 0, acc_b = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);

    unsigned char *buf = NULL;
    int len = 0;
    int cap = 0;
    uint64_t last_out = 0;

    while (has_a || has_b) {
        uint64_t next;
        if (has_a && has_b) {
            if (acc_a == acc_b) {
                next = acc_a;
                has_a = next_seq(&pa, ea, &acc_a);
                has_b = next_seq(&pb, eb, &acc_b);
            } else if (acc_a < acc_b) {
                next = acc_a;
                has_a = next_seq(&pa, ea, &acc_a);
            } else {
                next = acc_b;
                has_b = next_seq(&pb, eb, &acc_b);
            }
        } else if (has_a) {
            next = acc_a;
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            next = acc_b;
            has_b = next_seq(&pb, eb, &acc_b);
        }

        uint64_t delta = next - last_out;
        if (!append_varint_bytes(delta, &buf, &len, &cap)) {
            sqlite3_free(buf);
            return 0;
        }
        last_out = next;
    }

    *out = buf;
    *out_len = len;
    return 1;
}

static int fill_bitmap(const unsigned char *blob, int blob_len, uint64_t max_seq, uint64_t *out_bits) {
    if (!blob || blob_len <= 0) return 1;
    const uint8_t *p = blob;
    const uint8_t *end = blob + blob_len;
    uint64_t acc = 0;
    while (p < end) {
        uint64_t delta = read_varint(&p, end);
        acc += delta;
        if (acc > max_seq) break;
        uint64_t idx = acc >> 6;
        uint64_t bit = acc & 63;
        out_bits[idx] |= (uint64_t)1 << bit;
    }
    return 1;
}

/*
 * post_to_bitmap(blob, max_seq)
 *  - returnerer en bitvektor for postings (uint64_t array som BLOB)
 */
static void post_to_bitmap_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 2) {
        sqlite3_result_error(ctx, "post_to_bitmap(blob, max_seq) expects 2 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    int a_len = sqlite3_value_bytes(argv[0]);
    sqlite3_int64 max_seq = sqlite3_value_int64(argv[1]);
    if (max_seq < 0) {
        sqlite3_result_error(ctx, "post_to_bitmap: max_seq must be >= 0", -1);
        return;
    }

    uint64_t bits = (uint64_t)max_seq + 1;
    uint64_t words = (bits + 63) / 64;
    size_t byte_len = (size_t)words * sizeof(uint64_t);
    uint64_t *buf = sqlite3_malloc(byte_len);
    if (!buf) {
        sqlite3_result_error(ctx, "post_to_bitmap: OOM", -1);
        return;
    }
    memset(buf, 0, byte_len);
    fill_bitmap(a, a_len, (uint64_t)max_seq, buf);
    sqlite3_result_blob(ctx, buf, (int)byte_len, sqlite3_free);
}

/*
 * post_bigram_bitmap(blobA, blobB, dist, max_seq)
 *  - teller treff ved å mappe postings til bitmaps og gjøre shift+AND
 */
static void post_bigram_bitmap_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_bigram_bitmap(blobA, blobB, dist, max_seq) expects 4 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int dist = sqlite3_value_int(argv[2]);
    sqlite3_int64 max_seq = sqlite3_value_int64(argv[3]);

    if (max_seq < 0 || dist <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    uint64_t bits = (uint64_t)max_seq + 1;
    uint64_t words = (bits + 63) / 64;
    size_t byte_len = (size_t)words * sizeof(uint64_t);
    uint64_t *vec_a = sqlite3_malloc(byte_len);
    uint64_t *vec_b = sqlite3_malloc(byte_len);
    if (!vec_a || !vec_b) {
        sqlite3_free(vec_a);
        sqlite3_free(vec_b);
        sqlite3_result_error(ctx, "post_bigram_bitmap: OOM", -1);
        return;
    }
    memset(vec_a, 0, byte_len);
    memset(vec_b, 0, byte_len);

    fill_bitmap(a, a_len, (uint64_t)max_seq, vec_a);
    fill_bitmap(b, b_len, (uint64_t)max_seq, vec_b);

    uint64_t total = 0;
    int shift = dist & 63;
    for (uint64_t i = 0; i < words; i++) {
        uint64_t a_val = vec_a[i];
        uint64_t carry = 0;
        if (shift && i > 0) {
            carry = vec_a[i - 1] >> (64 - shift);
        }
        uint64_t a_shifted = shift ? ((a_val << shift) | carry) : a_val;
        uint64_t matches = a_shifted & vec_b[i];
        total += __builtin_popcountll(matches);
    }

    sqlite3_free(vec_a);
    sqlite3_free(vec_b);
    sqlite3_result_int64(ctx, (sqlite3_int64)total);
}

/*
 * post_bigram_bitmap_hybrid(blobA, blobB, dist, max_seq)
 *  - dekoder A til bitmap, itererer B og sjekker treff direkte
 */
static void post_bigram_bitmap_hybrid_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_bigram_bitmap_hybrid(blobA, blobB, dist, max_seq) expects 4 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int dist = sqlite3_value_int(argv[2]);
    sqlite3_int64 max_seq = sqlite3_value_int64(argv[3]);

    if (max_seq < 0 || dist <= 0 || !a || !b || a_len <= 0 || b_len <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    uint64_t bits = (uint64_t)max_seq + 1;
    uint64_t words = (bits + 63) / 64;
    size_t byte_len = (size_t)words * sizeof(uint64_t);
    uint64_t *vec_a = sqlite3_malloc(byte_len);
    if (!vec_a) {
        sqlite3_result_error(ctx, "post_bigram_bitmap_hybrid: OOM", -1);
        return;
    }
    memset(vec_a, 0, byte_len);
    fill_bitmap(a, a_len, (uint64_t)max_seq, vec_a);

    const uint8_t *pb = b;
    const uint8_t *eb = b + b_len;
    uint64_t acc_b = 0;
    uint64_t total = 0;
    while (pb < eb) {
        uint64_t delta = read_varint(&pb, eb);
        acc_b += delta;
        if (acc_b > (uint64_t)max_seq) break;
        if (acc_b >= (uint64_t)dist) {
            uint64_t pos = acc_b - (uint64_t)dist;
            uint64_t idx = pos >> 6;
            uint64_t bit = pos & 63;
            if (idx < words && (vec_a[idx] & ((uint64_t)1 << bit))) {
                total++;
            }
        }
    }

    sqlite3_free(vec_a);
    sqlite3_result_int64(ctx, (sqlite3_int64)total);
}

/*
 * bitmap_bigram_count(bitmapA, bitmapB, dist)
 *  - teller treff ved å gjøre shift+AND direkte på bitmaps
 */
static void bitmap_bigram_count_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 3) {
        sqlite3_result_error(ctx, "bitmap_bigram_count(bitmapA, bitmapB, dist) expects 3 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int dist = sqlite3_value_int(argv[2]);

    if (!a || !b || a_len <= 0 || b_len <= 0 || dist <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    int words_a = a_len / (int)sizeof(uint64_t);
    int words_b = b_len / (int)sizeof(uint64_t);
    int words = (words_a < words_b) ? words_a : words_b;
    if (words <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    const uint64_t *vec_a = (const uint64_t *)a;
    const uint64_t *vec_b = (const uint64_t *)b;
    int word_shift = dist >> 6;
    int bit_shift = dist & 63;
    uint64_t total = 0;

    for (int i = 0; i < words; i++) {
        int src = i - word_shift;
        uint64_t a_shifted = 0;
        if (src >= 0) {
            uint64_t a_val = vec_a[src];
            uint64_t carry = 0;
            if (bit_shift && src > 0) {
                carry = vec_a[src - 1] >> (64 - bit_shift);
            }
            a_shifted = bit_shift ? ((a_val << bit_shift) | carry) : a_val;
        }
        uint64_t matches = a_shifted & vec_b[i];
        total += __builtin_popcountll(matches);
    }

    sqlite3_result_int64(ctx, (sqlite3_int64)total);
}

/*
 * post_union(blobA, blobB)
 *  - returnerer unionen av to postingslister som ny delta/varint-BLOB
 */
static void post_union_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 2) {
        sqlite3_result_error(ctx, "post_union(blob, blob) expects 2 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);

    unsigned char *out = NULL;
    int out_len = 0;
    if (!union_blobs(a, a_len, b, b_len, &out, &out_len)) {
        sqlite3_result_error(ctx, "post_union: OOM", -1);
        return;
    }
    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
}

typedef struct {
    unsigned char *blob;
    int len;
} union_agg_ctx;

static void post_union_agg_step(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 1) {
        sqlite3_result_error(ctx, "post_union_agg(blob) expects 1 arg", -1);
        return;
    }
    const unsigned char *b = sqlite3_value_blob(argv[0]);
    int b_len = sqlite3_value_bytes(argv[0]);
    if (!b || b_len <= 0) return;

    union_agg_ctx *st = sqlite3_aggregate_context(ctx, sizeof(*st));
    if (!st) {
        sqlite3_result_error(ctx, "post_union_agg: OOM", -1);
        return;
    }
    if (!st->blob) {
        st->blob = sqlite3_malloc(b_len);
        if (!st->blob) {
            sqlite3_result_error(ctx, "post_union_agg: OOM", -1);
            return;
        }
        memcpy(st->blob, b, b_len);
        st->len = b_len;
        return;
    }

    unsigned char *out = NULL;
    int out_len = 0;
    if (!union_blobs(st->blob, st->len, b, b_len, &out, &out_len)) {
        sqlite3_result_error(ctx, "post_union_agg: OOM", -1);
        return;
    }
    sqlite3_free(st->blob);
    st->blob = out;
    st->len = out_len;
}

static void post_union_agg_final(sqlite3_context *ctx) {
    union_agg_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || !st->blob) {
        sqlite3_result_blob(ctx, "", 0, SQLITE_STATIC);
        return;
    }
    sqlite3_result_blob(ctx, st->blob, st->len, sqlite3_free);
    st->blob = NULL;
    st->len = 0;
}

/*
 * post_intersect_blob(blobA, blobB)
 *  - returnerer snittet av to postingslister som ny delta/varint-BLOB
 */
static void post_intersect_blob_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 2) {
        sqlite3_result_error(ctx, "post_intersect_blob(blob, blob) expects 2 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);

    if (!a || !b || a_len <= 0 || b_len <= 0) {
        sqlite3_result_blob(ctx, "", 0, SQLITE_STATIC);
        return;
    }

    const uint8_t *pa = a, *pb = b;
    const uint8_t *ea = a + a_len, *eb = b + b_len;
    uint64_t acc_a = 0, acc_b = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);

    unsigned char *out = NULL;
    int out_len = 0;
    int out_cap = 0;
    uint64_t last_out = 0;

    while (has_a && has_b) {
        if (acc_a == acc_b) {
            uint64_t delta = acc_a - last_out;
            if (!append_varint_bytes(delta, &out, &out_len, &out_cap)) {
                sqlite3_free(out);
                sqlite3_result_error(ctx, "post_intersect_blob: OOM", -1);
                return;
            }
            last_out = acc_a;
            has_a = next_seq(&pa, ea, &acc_a);
            has_b = next_seq(&pb, eb, &acc_b);
        } else if (acc_a < acc_b) {
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            has_b = next_seq(&pb, eb, &acc_b);
        }
    }

    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
}

/*
 * post_complement(blob, universe_blob)
 *  - returnerer komplementet av blob innenfor universe_blob som delta/varint-BLOB
 */
static void post_complement_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 2) {
        sqlite3_result_error(ctx, "post_complement(blob, universe_blob) expects 2 args", -1);
        return;
    }

    const unsigned char *sel = sqlite3_value_blob(argv[0]);
    const unsigned char *uni = sqlite3_value_blob(argv[1]);
    int sel_len = sqlite3_value_bytes(argv[0]);
    int uni_len = sqlite3_value_bytes(argv[1]);

    if (!uni || uni_len <= 0) {
        sqlite3_result_blob(ctx, "", 0, SQLITE_STATIC);
        return;
    }
    if (!sel || sel_len <= 0) {
        sqlite3_result_blob(ctx, (const void *)uni, uni_len, SQLITE_TRANSIENT);
        return;
    }

    const uint8_t *pa = uni;
    const uint8_t *pb = sel;
    const uint8_t *ea = uni + uni_len;
    const uint8_t *eb = sel + sel_len;
    uint64_t acc_a = 0, acc_b = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);

    unsigned char *out = NULL;
    int out_len = 0;
    int out_cap = 0;
    uint64_t last_out = 0;

    while (has_a) {
        while (has_b && acc_b < acc_a) {
            has_b = next_seq(&pb, eb, &acc_b);
        }
        if (has_b && acc_b == acc_a) {
            has_a = next_seq(&pa, ea, &acc_a);
            has_b = next_seq(&pb, eb, &acc_b);
            continue;
        }
        uint64_t delta = acc_a - last_out;
        if (!append_varint_bytes(delta, &out, &out_len, &out_cap)) {
            sqlite3_free(out);
            sqlite3_result_error(ctx, "post_complement: OOM", -1);
            return;
        }
        last_out = acc_a;
        has_a = next_seq(&pa, ea, &acc_a);
    }

    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
}

/*
 * post_intersect(blobA, blobB)
 *  - returnerer antall posisjoner som finnes i begge lister (eksakt match)
 */
static void post_intersect_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 2) {
        sqlite3_result_error(ctx, "post_intersect(blob, blob) expects 2 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);

    if (!a || !b || a_len <= 0 || b_len <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    const uint8_t *pa = a, *pb = b;
    const uint8_t *ea = a + a_len, *eb = b + b_len;
    uint64_t acc_a = 0, acc_b = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);
    int count = 0;

    while (has_a && has_b) {
        if (acc_a == acc_b) {
            count++;
            has_a = next_seq(&pa, ea, &acc_a);
            has_b = next_seq(&pb, eb, &acc_b);
        } else if (acc_a < acc_b) {
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            has_b = next_seq(&pb, eb, &acc_b);
        }
    }

    sqlite3_result_int(ctx, count);
}

/*
 * post_intersect_offset(blobA, blobB, off_min, off_max)
 *  - teller tilfeller der B ligger innenfor [off_min, off_max] i forhold til A
 *    dvs finnes seq_a og seq_b slik at seq_b - seq_a i [off_min, off_max]
 */
static void post_intersect_offset_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_intersect_offset(blob, blob, off_min, off_max) expects 4 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);

    if (!a || !b || a_len <= 0 || b_len <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    const uint8_t *pa = a, *pb = b;
    const uint8_t *ea = a + a_len, *eb = b + b_len;
    uint64_t acc_a = 0, acc_b = 0;

    // For enkelhet: naiv to-pointer med "window"
    int count = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);

    // Vi kan gjøre dette smartere senere (galloping/skip), men start enkelt.
    while (has_a && has_b) {
        int64_t diff = (int64_t)acc_b - (int64_t)acc_a;
        if (diff < off_min) {
            // B ligger for langt bak -> flytt B fram
            has_b = next_seq(&pb, eb, &acc_b);
        } else if (diff > off_max) {
            // B ligger for langt foran -> flytt A fram
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            // innenfor vindu
            count++;
            // flytt begge videre (eller bare B, avh. av semantikk)
            has_b = next_seq(&pb, eb, &acc_b);
        }
    }

    sqlite3_result_int(ctx, count);
}

/*
 * post_intersect_offset_sym(blobA, blobB, off_min, off_max)
 *  - symmetrisk variant: teller par og flytter begge ved treff
 *    (mindre følsom for rekkefølge mellom A/B)
 */
static void post_intersect_offset_sym_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_intersect_offset_sym(blob, blob, off_min, off_max) expects 4 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);

    if (!a || !b || a_len <= 0 || b_len <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    const uint8_t *pa = a, *pb = b;
    const uint8_t *ea = a + a_len, *eb = b + b_len;
    uint64_t acc_a = 0, acc_b = 0;

    int count = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);

    while (has_a && has_b) {
        int64_t diff = (int64_t)acc_b - (int64_t)acc_a;
        if (diff < off_min) {
            has_b = next_seq(&pb, eb, &acc_b);
        } else if (diff > off_max) {
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            count++;
            // Symmetrisk: flytt begge videre
            has_a = next_seq(&pa, ea, &acc_a);
            has_b = next_seq(&pb, eb, &acc_b);
        }
    }

    sqlite3_result_int(ctx, count);
}

/*
 * post_sample(blob, idx)
 *  - returnerer seq pa indeks idx (0-basert) i postingslista
 */
static void post_sample_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 2) {
        sqlite3_result_error(ctx, "post_sample(blob, idx) expects 2 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int idx = sqlite3_value_int(argv[1]);
    if (!a || a_len <= 0 || idx < 0) {
        sqlite3_result_null(ctx);
        return;
    }

    const uint8_t *p = a;
    const uint8_t *end = a + a_len;
    uint64_t acc = 0;
    int i = 0;

    while (p < end) {
        uint64_t delta = read_varint(&p, end);
        acc += delta;
        if (i == idx) {
            sqlite3_result_int64(ctx, (sqlite3_int64)acc);
            return;
        }
        i++;
    }

    // idx utenfor rekkevidde
    sqlite3_result_null(ctx);
}

/*
 * post_positions(blob)
 *  - returnerer alle posisjoner i postingslista som JSON-array
 */
static void post_positions_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 1) {
        sqlite3_result_error(ctx, "post_positions(blob) expects 1 arg", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    int a_len = sqlite3_value_bytes(argv[0]);
    if (!a || a_len <= 0) {
        sqlite3_result_text(ctx, "[]", -1, SQLITE_STATIC);
        return;
    }

    const uint8_t *p = a;
    const uint8_t *end = a + a_len;
    uint64_t acc = 0;

    char *buf = NULL;
    int len = 0;
    int cap = 0;
    if (!json_append_char(&buf, &len, &cap, '[')) {
        sqlite3_free(buf);
        sqlite3_result_error(ctx, "post_positions: OOM", -1);
        return;
    }

    int first = 1;
    while (p < end) {
        uint64_t delta = read_varint(&p, end);
        acc += delta;
        if (!first) {
            if (!json_append_char(&buf, &len, &cap, ',')) {
                sqlite3_free(buf);
                sqlite3_result_error(ctx, "post_positions: OOM", -1);
                return;
            }
        }
        first = 0;
        if (!json_append_int64(&buf, &len, &cap, (sqlite3_int64)acc)) {
            sqlite3_free(buf);
            sqlite3_result_error(ctx, "post_positions: OOM", -1);
            return;
        }
    }

    if (!json_append_char(&buf, &len, &cap, ']')) {
        sqlite3_free(buf);
        sqlite3_result_error(ctx, "post_positions: OOM", -1);
        return;
    }

    sqlite3_result_text(ctx, buf, len, sqlite3_free);
}

/*
 * post_complement_positions(blob, universe_blob)
 *  - returnerer komplementet av blob innenfor universe_blob som JSON-array
 */
static void post_complement_positions_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 2) {
        sqlite3_result_error(ctx, "post_complement_positions(blob, universe_blob) expects 2 args", -1);
        return;
    }

    const unsigned char *sel = sqlite3_value_blob(argv[0]);
    const unsigned char *uni = sqlite3_value_blob(argv[1]);
    int sel_len = sqlite3_value_bytes(argv[0]);
    int uni_len = sqlite3_value_bytes(argv[1]);
    if (!uni || uni_len <= 0) {
        sqlite3_result_text(ctx, "[]", -1, SQLITE_STATIC);
        return;
    }
    if (!sel || sel_len <= 0) {
        // return universe as-is
        sqlite3_value *tmpv[1];
        tmpv[0] = argv[1];
        post_positions_sqlite(ctx, 1, tmpv);
        return;
    }

    const uint8_t *pa = uni;
    const uint8_t *pb = sel;
    const uint8_t *ea = uni + uni_len;
    const uint8_t *eb = sel + sel_len;
    uint64_t acc_a = 0;
    uint64_t acc_b = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);

    char *buf = NULL;
    int len = 0;
    int cap = 0;
    if (!json_append_char(&buf, &len, &cap, '[')) {
        sqlite3_free(buf);
        sqlite3_result_error(ctx, "post_complement_positions: OOM", -1);
        return;
    }

    int first = 1;
    while (has_a) {
        while (has_b && acc_b < acc_a) {
            has_b = next_seq(&pb, eb, &acc_b);
        }
        if (has_b && acc_b == acc_a) {
            has_a = next_seq(&pa, ea, &acc_a);
            has_b = next_seq(&pb, eb, &acc_b);
            continue;
        }
        if (!first) {
            if (!json_append_char(&buf, &len, &cap, ',')) {
                sqlite3_free(buf);
                sqlite3_result_error(ctx, "post_complement_positions: OOM", -1);
                return;
            }
        }
        first = 0;
        if (!json_append_int64(&buf, &len, &cap, (sqlite3_int64)acc_a)) {
            sqlite3_free(buf);
            sqlite3_result_error(ctx, "post_complement_positions: OOM", -1);
            return;
        }
        has_a = next_seq(&pa, ea, &acc_a);
    }

    if (!json_append_char(&buf, &len, &cap, ']')) {
        sqlite3_free(buf);
        sqlite3_result_error(ctx, "post_complement_positions: OOM", -1);
        return;
    }

    sqlite3_result_text(ctx, buf, len, sqlite3_free);
}

/*
 * post_near_positions(blobA, blobB, off_min, off_max)
 *  - returnerer posisjoner i A der det finnes en B innenfor [off_min, off_max]
 */
static void post_near_positions_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_near_positions(blob, blob, off_min, off_max) expects 4 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);

    if (!a || !b || a_len <= 0 || b_len <= 0) {
        sqlite3_result_text(ctx, "[]", -1, SQLITE_STATIC);
        return;
    }

    const uint8_t *pa = a, *pb = b;
    const uint8_t *ea = a + a_len, *eb = b + b_len;
    uint64_t acc_a = 0, acc_b = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);

    char *buf = NULL;
    int len = 0;
    int cap = 0;
    if (!json_append_char(&buf, &len, &cap, '[')) {
        sqlite3_free(buf);
        sqlite3_result_error(ctx, "post_near_positions: OOM", -1);
        return;
    }

    int first = 1;
    while (has_a && has_b) {
        int64_t diff = (int64_t)acc_b - (int64_t)acc_a;
        if (diff < off_min) {
            has_b = next_seq(&pb, eb, &acc_b);
        } else if (diff > off_max) {
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            if (!first) {
                if (!json_append_char(&buf, &len, &cap, ',')) {
                    sqlite3_free(buf);
                    sqlite3_result_error(ctx, "post_near_positions: OOM", -1);
                    return;
                }
            }
            first = 0;
            if (!json_append_int64(&buf, &len, &cap, (sqlite3_int64)acc_a)) {
                sqlite3_free(buf);
                sqlite3_result_error(ctx, "post_near_positions: OOM", -1);
                return;
            }
            has_a = next_seq(&pa, ea, &acc_a);
        }
    }

    if (!json_append_char(&buf, &len, &cap, ']')) {
        sqlite3_free(buf);
        sqlite3_result_error(ctx, "post_near_positions: OOM", -1);
        return;
    }

    sqlite3_result_text(ctx, buf, len, sqlite3_free);
}

/*
 * post_near_count(blobA, blobB, off_min, off_max)
 *  - returnerer antall posisjoner i A der det finnes en B innenfor [off_min, off_max]
 */
static void post_near_count_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_near_count(blob, blob, off_min, off_max) expects 4 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);

    if (!a || !b || a_len <= 0 || b_len <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    const uint8_t *pa = a, *pb = b;
    const uint8_t *ea = a + a_len, *eb = b + b_len;
    uint64_t acc_a = 0, acc_b = 0;
    int has_a = next_seq(&pa, ea, &acc_a);
    int has_b = next_seq(&pb, eb, &acc_b);
    int count = 0;

    while (has_a && has_b) {
        int64_t diff = (int64_t)acc_b - (int64_t)acc_a;
        if (diff < off_min) {
            has_b = next_seq(&pb, eb, &acc_b);
        } else if (diff > off_max) {
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            count++;
            has_a = next_seq(&pa, ea, &acc_a);
        }
    }

    sqlite3_result_int(ctx, count);
}

// Entry point for sqlite3_load_extension
int sqlite3_postings_init(
    sqlite3 *db,
    char **pzErrMsg,
    const sqlite3_api_routines *pApi
){
    SQLITE_EXTENSION_INIT2(pApi);
    int rc = SQLITE_OK;

    rc = sqlite3_create_function(
        db, "post_intersect", 2,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_intersect_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_intersect_blob", 2,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_intersect_blob_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_intersect_offset", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_intersect_offset_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_intersect_offset_sym", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_intersect_offset_sym_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_sample", 2,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_sample_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_positions", 1,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_positions_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_complement_positions", 2,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_complement_positions_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_positions", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_near_positions_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_count", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_near_count_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_union", 2,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_union_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_union_agg", 1,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_union_agg_step, post_union_agg_final
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_complement", 2,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_complement_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_to_bitmap", 2,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_to_bitmap_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_bigram_bitmap", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_bigram_bitmap_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_bigram_bitmap_hybrid", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_bigram_bitmap_hybrid_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "bitmap_bigram_count", 3,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, bitmap_bigram_count_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    return SQLITE_OK;
}
