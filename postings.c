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

/*
 * post_count(blob)
 *  - returnerer antall postings i blobben
 */
static void post_count_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 1) {
        sqlite3_result_error(ctx, "post_count(blob) expects 1 arg", -1);
        return;
    }
    const unsigned char *a = sqlite3_value_blob(argv[0]);
    int a_len = sqlite3_value_bytes(argv[0]);
    if (!a || a_len <= 0) {
        sqlite3_result_int64(ctx, 0);
        return;
    }

    const uint8_t *p = a;
    const uint8_t *end = a + a_len;
    uint64_t acc = 0;
    sqlite3_int64 count = 0;
    while (p < end) {
        uint64_t delta = read_varint(&p, end);
        acc += delta;
        count++;
    }
    sqlite3_result_int64(ctx, count);
}

typedef struct {
    unsigned char **blobs;
    int *lens;
    int count;
    int cap;
} union_agg_ctx;

typedef struct {
    const uint8_t *ptr;
    const uint8_t *end;
    uint64_t acc;
    int active;
} blob_iter;

typedef struct {
    unsigned char **blobs1;
    int *lens1;
    int count1;
    int cap1;
    unsigned char **blobs2;
    int *lens2;
    int count2;
    int cap2;
    int off_min;
    int off_max;
    int has_offsets;
    int chunk_size;
} near_groups_ctx;

typedef struct {
    unsigned char ***blobs;
    int **lens;
    int *count;
    int *cap;
    int groups_cap;
    int max_grp;
    int off_min;
    int off_max;
    int has_offsets;
    int chunk_size;
} near_multi_ctx;

static int append_blob_group(
    unsigned char ***blobs,
    int **lens,
    int *count,
    int *cap,
    const unsigned char *src,
    int src_len
) {
    if (*count == *cap) {
        int new_cap = *cap ? *cap * 2 : 8;
        unsigned char **new_blobs = sqlite3_malloc(new_cap * sizeof(*new_blobs));
        int *new_lens = sqlite3_malloc(new_cap * sizeof(*new_lens));
        if (!new_blobs || !new_lens) {
            sqlite3_free(new_blobs);
            sqlite3_free(new_lens);
            return 0;
        }
        if (*count > 0) {
            memcpy(new_blobs, *blobs, (*count) * sizeof(*new_blobs));
            memcpy(new_lens, *lens, (*count) * sizeof(*new_lens));
        }
        sqlite3_free(*blobs);
        sqlite3_free(*lens);
        *blobs = new_blobs;
        *lens = new_lens;
        *cap = new_cap;
    }
    unsigned char *copy = sqlite3_malloc(src_len);
    if (!copy) return 0;
    memcpy(copy, src, src_len);
    (*blobs)[*count] = copy;
    (*lens)[*count] = src_len;
    *count += 1;
    return 1;
}

static void free_group_arrays(unsigned char **blobs, int count, int *lens) {
    if (blobs) {
        for (int i = 0; i < count; i++) {
            sqlite3_free(blobs[i]);
        }
        sqlite3_free(blobs);
    }
    sqlite3_free(lens);
}

static int mask_range(int start_bit, int end_bit, uint64_t *mask_out) {
    int len = end_bit - start_bit + 1;
    if (len <= 0) return 0;
    if (len >= 64) {
        *mask_out = ~0ULL;
        return 1;
    }
    *mask_out = ((1ULL << len) - 1ULL) << start_bit;
    return 1;
}

static int range_has_bits(
    const uint64_t *bits,
    uint64_t chunk_start,
    uint64_t chunk_end,
    int chunk_words,
    int64_t start,
    int64_t end
) {
    if (end < start) return 0;
    if (end < 0) return 0;
    if ((uint64_t)start > chunk_end) return 0;
    if ((uint64_t)end < chunk_start) return 0;

    uint64_t local_start = (start < (int64_t)chunk_start) ? 0 : (uint64_t)start - chunk_start;
    uint64_t local_end = (uint64_t)end > chunk_end ? (chunk_end - chunk_start) : (uint64_t)end - chunk_start;

    int ws = (int)(local_start >> 6);
    int we = (int)(local_end >> 6);
    int sb = (int)(local_start & 63);
    int eb = (int)(local_end & 63);
    if (ws < 0) ws = 0;
    if (we >= chunk_words) we = chunk_words - 1;
    if (ws == we) {
        uint64_t mask = 0;
        mask_range(sb, eb, &mask);
        return (bits[ws] & mask) != 0;
    }
    uint64_t mask_start = 0;
    mask_range(sb, 63, &mask_start);
    if (bits[ws] & mask_start) return 1;
    for (int i = ws + 1; i < we; i++) {
        if (bits[i]) return 1;
    }
    uint64_t mask_end = 0;
    mask_range(0, eb, &mask_end);
    return (bits[we] & mask_end) != 0;
}

static void clear_bits(uint64_t *bits, int words) {
    memset(bits, 0, (size_t)words * sizeof(uint64_t));
}

static void set_bit(uint64_t *bits, uint64_t chunk_start, uint64_t pos) {
    uint64_t local = pos - chunk_start;
    int word = (int)(local >> 6);
    int bit = (int)(local & 63);
    bits[word] |= (1ULL << bit);
}

static int group_next(blob_iter *iters, int n, uint64_t *out) {
    uint64_t min_val = UINT64_MAX;
    int any_active = 0;
    for (int i = 0; i < n; i++) {
        if (iters[i].active) {
            any_active = 1;
            if (iters[i].acc < min_val) {
                min_val = iters[i].acc;
            }
        }
    }
    if (!any_active) return 0;
    *out = min_val;
    for (int i = 0; i < n; i++) {
        if (iters[i].active && iters[i].acc == min_val) {
            iters[i].active = next_seq(&iters[i].ptr, iters[i].end, &iters[i].acc);
        }
    }
    return 1;
}

static int ensure_multi_group_capacity(near_multi_ctx *st, int grp) {
    if (grp < st->groups_cap) return 1;
    int new_cap = st->groups_cap ? st->groups_cap : 8;
    while (new_cap <= grp) new_cap *= 2;

    unsigned char ***new_blobs = sqlite3_malloc((size_t)new_cap * sizeof(*new_blobs));
    int **new_lens = sqlite3_malloc((size_t)new_cap * sizeof(*new_lens));
    int *new_count = sqlite3_malloc((size_t)new_cap * sizeof(*new_count));
    int *new_cap_arr = sqlite3_malloc((size_t)new_cap * sizeof(*new_cap_arr));
    if (!new_blobs || !new_lens || !new_count || !new_cap_arr) {
        sqlite3_free(new_blobs);
        sqlite3_free(new_lens);
        sqlite3_free(new_count);
        sqlite3_free(new_cap_arr);
        return 0;
    }
    for (int i = 0; i < new_cap; i++) {
        new_blobs[i] = NULL;
        new_lens[i] = NULL;
        new_count[i] = 0;
        new_cap_arr[i] = 0;
    }
    for (int i = 0; i < st->groups_cap; i++) {
        new_blobs[i] = st->blobs[i];
        new_lens[i] = st->lens[i];
        new_count[i] = st->count[i];
        new_cap_arr[i] = st->cap[i];
    }
    sqlite3_free(st->blobs);
    sqlite3_free(st->lens);
    sqlite3_free(st->count);
    sqlite3_free(st->cap);
    st->blobs = new_blobs;
    st->lens = new_lens;
    st->count = new_count;
    st->cap = new_cap_arr;
    st->groups_cap = new_cap;
    return 1;
}

static void free_multi_groups(near_multi_ctx *st) {
    if (!st || !st->blobs) return;
    for (int g = 0; g <= st->max_grp && g < st->groups_cap; g++) {
        if (st->count[g] > 0 || st->blobs[g] || st->lens[g]) {
            free_group_arrays(st->blobs[g], st->count[g], st->lens[g]);
        }
        st->blobs[g] = NULL;
        st->lens[g] = NULL;
        st->count[g] = 0;
        st->cap[g] = 0;
    }
}

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
    if (st->count == st->cap) {
        int new_cap = st->cap ? st->cap * 2 : 8;
        unsigned char **new_blobs = sqlite3_malloc(new_cap * sizeof(*new_blobs));
        int *new_lens = sqlite3_malloc(new_cap * sizeof(*new_lens));
        if (!new_blobs || !new_lens) {
            sqlite3_free(new_blobs);
            sqlite3_free(new_lens);
            sqlite3_result_error(ctx, "post_union_agg: OOM", -1);
            return;
        }
        if (st->count > 0) {
            memcpy(new_blobs, st->blobs, st->count * sizeof(*new_blobs));
            memcpy(new_lens, st->lens, st->count * sizeof(*new_lens));
        }
        sqlite3_free(st->blobs);
        sqlite3_free(st->lens);
        st->blobs = new_blobs;
        st->lens = new_lens;
        st->cap = new_cap;
    }
    unsigned char *copy = sqlite3_malloc(b_len);
    if (!copy) {
        sqlite3_result_error(ctx, "post_union_agg: OOM", -1);
        return;
    }
    memcpy(copy, b, b_len);
    st->blobs[st->count] = copy;
    st->lens[st->count] = b_len;
    st->count += 1;
}

static void post_union_agg_final(sqlite3_context *ctx) {
    union_agg_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || st->count <= 0) {
        sqlite3_result_blob(ctx, "", 0, SQLITE_STATIC);
        return;
    }
    blob_iter *iters = sqlite3_malloc(sizeof(*iters) * st->count);
    if (!iters) {
        sqlite3_result_error(ctx, "post_union_agg: OOM", -1);
        return;
    }
    for (int i = 0; i < st->count; i++) {
        iters[i].ptr = st->blobs[i];
        iters[i].end = st->blobs[i] + st->lens[i];
        iters[i].acc = 0;
        iters[i].active = next_seq(&iters[i].ptr, iters[i].end, &iters[i].acc);
    }

    unsigned char *out = NULL;
    int out_len = 0;
    int out_cap = 0;
    uint64_t last_out = 0;
    int has_out = 0;

    while (1) {
        uint64_t min_val = UINT64_MAX;
        int any_active = 0;
        for (int i = 0; i < st->count; i++) {
            if (iters[i].active) {
                any_active = 1;
                if (iters[i].acc < min_val) {
                    min_val = iters[i].acc;
                }
            }
        }
        if (!any_active) break;

        if (!has_out || min_val != last_out) {
            uint64_t delta = has_out ? (min_val - last_out) : min_val;
            if (!append_varint_bytes(delta, &out, &out_len, &out_cap)) {
                sqlite3_free(out);
                sqlite3_free(iters);
                for (int i = 0; i < st->count; i++) {
                    sqlite3_free(st->blobs[i]);
                }
                sqlite3_free(st->blobs);
                sqlite3_free(st->lens);
                st->blobs = NULL;
                st->lens = NULL;
                st->count = 0;
                st->cap = 0;
                sqlite3_result_error(ctx, "post_union_agg: OOM", -1);
                return;
            }
            last_out = min_val;
            has_out = 1;
        }

        for (int i = 0; i < st->count; i++) {
            if (iters[i].active && iters[i].acc == min_val) {
                iters[i].active = next_seq(&iters[i].ptr, iters[i].end, &iters[i].acc);
            }
        }
    }

    for (int i = 0; i < st->count; i++) {
        sqlite3_free(st->blobs[i]);
    }
    sqlite3_free(st->blobs);
    sqlite3_free(st->lens);
    st->blobs = NULL;
    st->lens = NULL;
    st->count = 0;
    st->cap = 0;
    sqlite3_free(iters);

    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
}

static void post_near_count_groups_step(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_near_count_groups(grp, blob, off_min, off_max) expects 4 args", -1);
        return;
    }
    int grp = sqlite3_value_int(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);
    if (!b || b_len <= 0) return;

    near_groups_ctx *st = sqlite3_aggregate_context(ctx, sizeof(*st));
    if (!st) {
        sqlite3_result_error(ctx, "post_near_count_groups: OOM", -1);
        return;
    }
    if (!st->has_offsets) {
        st->off_min = off_min;
        st->off_max = off_max;
        st->has_offsets = 1;
        st->chunk_size = sqlite3_value_int(argv[3]);
    }
    if (grp == 1) {
        if (!append_blob_group(&st->blobs1, &st->lens1, &st->count1, &st->cap1, b, b_len)) {
            sqlite3_result_error(ctx, "post_near_count_groups: OOM", -1);
        }
    } else if (grp == 2) {
        if (!append_blob_group(&st->blobs2, &st->lens2, &st->count2, &st->cap2, b, b_len)) {
            sqlite3_result_error(ctx, "post_near_count_groups: OOM", -1);
        }
    }
}

static void post_near_count_groups_final(sqlite3_context *ctx) {
    near_groups_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || st->count1 == 0 || st->count2 == 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    blob_iter *iters1 = sqlite3_malloc(sizeof(*iters1) * st->count1);
    blob_iter *iters2 = sqlite3_malloc(sizeof(*iters2) * st->count2);
    if (!iters1 || !iters2) {
        sqlite3_free(iters1);
        sqlite3_free(iters2);
        sqlite3_result_error(ctx, "post_near_count_groups: OOM", -1);
        return;
    }
    for (int i = 0; i < st->count1; i++) {
        iters1[i].ptr = st->blobs1[i];
        iters1[i].end = st->blobs1[i] + st->lens1[i];
        iters1[i].acc = 0;
        iters1[i].active = next_seq(&iters1[i].ptr, iters1[i].end, &iters1[i].acc);
    }
    for (int i = 0; i < st->count2; i++) {
        iters2[i].ptr = st->blobs2[i];
        iters2[i].end = st->blobs2[i] + st->lens2[i];
        iters2[i].acc = 0;
        iters2[i].active = next_seq(&iters2[i].ptr, iters2[i].end, &iters2[i].acc);
    }

    uint64_t a_val = 0, b_val = 0;
    int has_a = group_next(iters1, st->count1, &a_val);
    int has_b = group_next(iters2, st->count2, &b_val);
    int count = 0;
    while (has_a && has_b) {
        int64_t diff = (int64_t)b_val - (int64_t)a_val;
        if (diff < st->off_min) {
            has_b = group_next(iters2, st->count2, &b_val);
        } else if (diff > st->off_max) {
            has_a = group_next(iters1, st->count1, &a_val);
        } else {
            count++;
            has_a = group_next(iters1, st->count1, &a_val);
        }
    }

    sqlite3_free(iters1);
    sqlite3_free(iters2);
    free_group_arrays(st->blobs1, st->count1, st->lens1);
    free_group_arrays(st->blobs2, st->count2, st->lens2);
    st->blobs1 = NULL;
    st->lens1 = NULL;
    st->count1 = 0;
    st->cap1 = 0;
    st->blobs2 = NULL;
    st->lens2 = NULL;
    st->count2 = 0;
    st->cap2 = 0;

    sqlite3_result_int(ctx, count);
}

static void post_near_positions_groups_step(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    post_near_count_groups_step(ctx, argc, argv);
}

static void post_near_count_bitmap_groups_step(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 5) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_groups(grp, blob, off_min, off_max, chunk_size) expects 5 args", -1);
        return;
    }
    int grp = sqlite3_value_int(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);
    int chunk_size = sqlite3_value_int(argv[4]);
    if (!b || b_len <= 0) return;

    near_groups_ctx *st = sqlite3_aggregate_context(ctx, sizeof(*st));
    if (!st) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_groups: OOM", -1);
        return;
    }
    if (!st->has_offsets) {
        st->off_min = off_min;
        st->off_max = off_max;
        st->has_offsets = 1;
        st->chunk_size = chunk_size;
    }
    if (grp == 1) {
        if (!append_blob_group(&st->blobs1, &st->lens1, &st->count1, &st->cap1, b, b_len)) {
            sqlite3_result_error(ctx, "post_near_count_bitmap_groups: OOM", -1);
        }
    } else if (grp == 2) {
        if (!append_blob_group(&st->blobs2, &st->lens2, &st->count2, &st->cap2, b, b_len)) {
            sqlite3_result_error(ctx, "post_near_count_bitmap_groups: OOM", -1);
        }
    }
}

static int run_bitmap_near(
    near_groups_ctx *st,
    int want_positions,
    unsigned char **out_blob,
    int *out_len
) {
    if (st->count1 == 0 || st->count2 == 0) {
        *out_blob = NULL;
        *out_len = 0;
        return 1;
    }
    int chunk_size = st->chunk_size > 0 ? st->chunk_size : 4096;
    if (chunk_size % 64 != 0) {
        chunk_size = ((chunk_size + 63) / 64) * 64;
    }
    int words = chunk_size / 64;

    blob_iter *iters1 = sqlite3_malloc(sizeof(*iters1) * st->count1);
    blob_iter *iters2 = sqlite3_malloc(sizeof(*iters2) * st->count2);
    if (!iters1 || !iters2) {
        sqlite3_free(iters1);
        sqlite3_free(iters2);
        return 0;
    }
    for (int i = 0; i < st->count1; i++) {
        iters1[i].ptr = st->blobs1[i];
        iters1[i].end = st->blobs1[i] + st->lens1[i];
        iters1[i].acc = 0;
        iters1[i].active = next_seq(&iters1[i].ptr, iters1[i].end, &iters1[i].acc);
    }
    for (int i = 0; i < st->count2; i++) {
        iters2[i].ptr = st->blobs2[i];
        iters2[i].end = st->blobs2[i] + st->lens2[i];
        iters2[i].acc = 0;
        iters2[i].active = next_seq(&iters2[i].ptr, iters2[i].end, &iters2[i].acc);
    }

    uint64_t a_pos = 0, b_pos = 0;
    int has_a = group_next(iters1, st->count1, &a_pos);
    int has_b = group_next(iters2, st->count2, &b_pos);
    if (!has_a || !has_b) {
        sqlite3_free(iters1);
        sqlite3_free(iters2);
        *out_blob = NULL;
        *out_len = 0;
        return 1;
    }

    uint64_t *bits_prev = sqlite3_malloc(sizeof(uint64_t) * words);
    uint64_t *bits_curr = sqlite3_malloc(sizeof(uint64_t) * words);
    uint64_t *bits_next = sqlite3_malloc(sizeof(uint64_t) * words);
    if (!bits_prev || !bits_curr || !bits_next) {
        sqlite3_free(bits_prev);
        sqlite3_free(bits_curr);
        sqlite3_free(bits_next);
        sqlite3_free(iters1);
        sqlite3_free(iters2);
        return 0;
    }

    int64_t curr_idx = (int64_t)(a_pos / (uint64_t)chunk_size);
    int64_t prev_idx = curr_idx - 1;
    int64_t next_idx = curr_idx + 1;
    clear_bits(bits_prev, words);
    clear_bits(bits_curr, words);
    clear_bits(bits_next, words);

    // fill prev, curr, next in order using B positions
    if (prev_idx >= 0) {
        uint64_t prev_start = (uint64_t)prev_idx * (uint64_t)chunk_size;
        uint64_t prev_end = prev_start + (uint64_t)chunk_size - 1;
        while (has_b && b_pos <= prev_end) {
            if (b_pos >= prev_start) set_bit(bits_prev, prev_start, b_pos);
            has_b = group_next(iters2, st->count2, &b_pos);
        }
    }
    {
        uint64_t curr_start = (uint64_t)curr_idx * (uint64_t)chunk_size;
        uint64_t curr_end = curr_start + (uint64_t)chunk_size - 1;
        while (has_b && b_pos <= curr_end) {
            if (b_pos >= curr_start) set_bit(bits_curr, curr_start, b_pos);
            has_b = group_next(iters2, st->count2, &b_pos);
        }
    }
    {
        uint64_t next_start = (uint64_t)next_idx * (uint64_t)chunk_size;
        uint64_t next_end = next_start + (uint64_t)chunk_size - 1;
        while (has_b && b_pos <= next_end) {
            if (b_pos >= next_start) set_bit(bits_next, next_start, b_pos);
            has_b = group_next(iters2, st->count2, &b_pos);
        }
    }

    unsigned char *out = NULL;
    int out_len_local = 0;
    int out_cap = 0;
    uint64_t last_out = 0;
    int has_out = 0;
    int count = 0;

    while (has_a) {
        int64_t idx = (int64_t)(a_pos / (uint64_t)chunk_size);
        while (idx > curr_idx) {
            // rotate chunks forward
            uint64_t *tmp = bits_prev;
            bits_prev = bits_curr;
            bits_curr = bits_next;
            bits_next = tmp;
            prev_idx = curr_idx;
            curr_idx = next_idx;
            next_idx = curr_idx + 1;
            clear_bits(bits_next, words);
            uint64_t next_start = (uint64_t)next_idx * (uint64_t)chunk_size;
            uint64_t next_end = next_start + (uint64_t)chunk_size - 1;
            while (has_b && b_pos <= next_end) {
                if (b_pos >= next_start) set_bit(bits_next, next_start, b_pos);
                has_b = group_next(iters2, st->count2, &b_pos);
            }
        }

        int64_t start = (int64_t)a_pos + (int64_t)st->off_min;
        int64_t end = (int64_t)a_pos + (int64_t)st->off_max;
        if (end >= 0) {
            uint64_t curr_start = (uint64_t)curr_idx * (uint64_t)chunk_size;
            uint64_t curr_end = curr_start + (uint64_t)chunk_size - 1;
            uint64_t prev_start = curr_start - (uint64_t)chunk_size;
            uint64_t prev_end = curr_start - 1;
            uint64_t next_start = curr_end + 1;
            uint64_t next_end = next_start + (uint64_t)chunk_size - 1;

            int hit = 0;
            if (prev_idx >= 0) {
                hit = range_has_bits(bits_prev, prev_start, prev_end, words, start, end);
            }
            if (!hit) {
                hit = range_has_bits(bits_curr, curr_start, curr_end, words, start, end);
            }
            if (!hit) {
                hit = range_has_bits(bits_next, next_start, next_end, words, start, end);
            }
            if (hit) {
                count++;
                if (want_positions) {
                    uint64_t delta = has_out ? (a_pos - last_out) : a_pos;
                    if (!append_varint_bytes(delta, &out, &out_len_local, &out_cap)) {
                        sqlite3_free(out);
                        sqlite3_free(bits_prev);
                        sqlite3_free(bits_curr);
                        sqlite3_free(bits_next);
                        sqlite3_free(iters1);
                        sqlite3_free(iters2);
                        return 0;
                    }
                    last_out = a_pos;
                    has_out = 1;
                }
            }
        }
        has_a = group_next(iters1, st->count1, &a_pos);
    }

    sqlite3_free(bits_prev);
    sqlite3_free(bits_curr);
    sqlite3_free(bits_next);
    sqlite3_free(iters1);
    sqlite3_free(iters2);
    if (!want_positions) {
        *out_blob = NULL;
        *out_len = count;
    } else {
        *out_blob = out;
        *out_len = out_len_local;
    }
    return 1;
}

typedef struct {
    int grp;
    blob_iter *iters;
    int n_iters;
    int has_b;
    uint64_t b_pos;
    uint64_t *bits_prev;
    uint64_t *bits_curr;
    uint64_t *bits_next;
    int64_t prev_idx;
    int64_t curr_idx;
    int64_t next_idx;
} multi_group_state;

static int init_multi_group_state(
    multi_group_state *gs,
    near_multi_ctx *st,
    int grp,
    int words,
    int chunk_size,
    int64_t anchor_idx
) {
    gs->grp = grp;
    gs->n_iters = st->count[grp];
    gs->iters = sqlite3_malloc(sizeof(*gs->iters) * gs->n_iters);
    gs->bits_prev = sqlite3_malloc(sizeof(uint64_t) * words);
    gs->bits_curr = sqlite3_malloc(sizeof(uint64_t) * words);
    gs->bits_next = sqlite3_malloc(sizeof(uint64_t) * words);
    if (!gs->iters || !gs->bits_prev || !gs->bits_curr || !gs->bits_next) {
        sqlite3_free(gs->iters);
        sqlite3_free(gs->bits_prev);
        sqlite3_free(gs->bits_curr);
        sqlite3_free(gs->bits_next);
        gs->iters = NULL;
        gs->bits_prev = gs->bits_curr = gs->bits_next = NULL;
        return 0;
    }
    for (int i = 0; i < gs->n_iters; i++) {
        gs->iters[i].ptr = st->blobs[grp][i];
        gs->iters[i].end = st->blobs[grp][i] + st->lens[grp][i];
        gs->iters[i].acc = 0;
        gs->iters[i].active = next_seq(&gs->iters[i].ptr, gs->iters[i].end, &gs->iters[i].acc);
    }
    gs->has_b = group_next(gs->iters, gs->n_iters, &gs->b_pos);
    gs->curr_idx = anchor_idx;
    gs->prev_idx = anchor_idx - 1;
    gs->next_idx = anchor_idx + 1;
    clear_bits(gs->bits_prev, words);
    clear_bits(gs->bits_curr, words);
    clear_bits(gs->bits_next, words);

    if (gs->prev_idx >= 0) {
        uint64_t prev_start = (uint64_t)gs->prev_idx * (uint64_t)chunk_size;
        uint64_t prev_end = prev_start + (uint64_t)chunk_size - 1;
        while (gs->has_b && gs->b_pos <= prev_end) {
            if (gs->b_pos >= prev_start) set_bit(gs->bits_prev, prev_start, gs->b_pos);
            gs->has_b = group_next(gs->iters, gs->n_iters, &gs->b_pos);
        }
    }
    {
        uint64_t curr_start = (uint64_t)gs->curr_idx * (uint64_t)chunk_size;
        uint64_t curr_end = curr_start + (uint64_t)chunk_size - 1;
        while (gs->has_b && gs->b_pos <= curr_end) {
            if (gs->b_pos >= curr_start) set_bit(gs->bits_curr, curr_start, gs->b_pos);
            gs->has_b = group_next(gs->iters, gs->n_iters, &gs->b_pos);
        }
    }
    {
        uint64_t next_start = (uint64_t)gs->next_idx * (uint64_t)chunk_size;
        uint64_t next_end = next_start + (uint64_t)chunk_size - 1;
        while (gs->has_b && gs->b_pos <= next_end) {
            if (gs->b_pos >= next_start) set_bit(gs->bits_next, next_start, gs->b_pos);
            gs->has_b = group_next(gs->iters, gs->n_iters, &gs->b_pos);
        }
    }
    return 1;
}

static void free_multi_group_state(multi_group_state *gs) {
    sqlite3_free(gs->iters);
    sqlite3_free(gs->bits_prev);
    sqlite3_free(gs->bits_curr);
    sqlite3_free(gs->bits_next);
    gs->iters = NULL;
    gs->bits_prev = gs->bits_curr = gs->bits_next = NULL;
}

static void rotate_multi_group_state(multi_group_state *gs, int64_t target_idx, int words, int chunk_size) {
    while (target_idx > gs->curr_idx) {
        uint64_t *tmp = gs->bits_prev;
        gs->bits_prev = gs->bits_curr;
        gs->bits_curr = gs->bits_next;
        gs->bits_next = tmp;
        gs->prev_idx = gs->curr_idx;
        gs->curr_idx = gs->next_idx;
        gs->next_idx = gs->curr_idx + 1;
        clear_bits(gs->bits_next, words);
        uint64_t next_start = (uint64_t)gs->next_idx * (uint64_t)chunk_size;
        uint64_t next_end = next_start + (uint64_t)chunk_size - 1;
        while (gs->has_b && gs->b_pos <= next_end) {
            if (gs->b_pos >= next_start) set_bit(gs->bits_next, next_start, gs->b_pos);
            gs->has_b = group_next(gs->iters, gs->n_iters, &gs->b_pos);
        }
    }
}

static int group_hit_in_window(
    multi_group_state *gs,
    int words,
    int chunk_size,
    int64_t start,
    int64_t end
) {
    if (end < start || end < 0) return 0;
    uint64_t curr_start = (uint64_t)gs->curr_idx * (uint64_t)chunk_size;
    uint64_t curr_end = curr_start + (uint64_t)chunk_size - 1;
    int hit = 0;
    if (gs->prev_idx >= 0) {
        uint64_t prev_start = curr_start - (uint64_t)chunk_size;
        uint64_t prev_end = curr_start - 1;
        hit = range_has_bits(gs->bits_prev, prev_start, prev_end, words, start, end);
    }
    if (!hit) {
        hit = range_has_bits(gs->bits_curr, curr_start, curr_end, words, start, end);
    }
    if (!hit) {
        uint64_t next_start = curr_end + 1;
        uint64_t next_end = next_start + (uint64_t)chunk_size - 1;
        hit = range_has_bits(gs->bits_next, next_start, next_end, words, start, end);
    }
    return hit;
}

static int any_bits_set(const uint64_t *bits, int words) {
    for (int i = 0; i < words; i++) {
        if (bits[i]) return 1;
    }
    return 0;
}

static void build_anchor_hit_mask_for_group(
    const multi_group_state *gs,
    int words,
    int chunk_size,
    int off_min,
    int off_max,
    uint64_t *out_bits
) {
    clear_bits(out_bits, words);
    uint64_t curr_start = (uint64_t)gs->curr_idx * (uint64_t)chunk_size;
    uint64_t curr_end = curr_start + (uint64_t)chunk_size - 1;

    const uint64_t *src_bits[3] = { gs->bits_prev, gs->bits_curr, gs->bits_next };
    uint64_t src_start[3] = {
        curr_start - (uint64_t)chunk_size,
        curr_start,
        curr_start + (uint64_t)chunk_size
    };
    for (int si = 0; si < 3; si++) {
        if (si == 0 && gs->prev_idx < 0) continue;
        const uint64_t *bits = src_bits[si];
        uint64_t base = src_start[si];
        for (int wi = 0; wi < words; wi++) {
            uint64_t w = bits[wi];
            while (w) {
                int bit = __builtin_ctzll(w);
                uint64_t q = base + ((uint64_t)wi << 6) + (uint64_t)bit;
                for (int d = off_min; d <= off_max; d++) {
                    int64_t p = (int64_t)q - (int64_t)d;
                    if (p >= (int64_t)curr_start && p <= (int64_t)curr_end) {
                        set_bit(out_bits, curr_start, (uint64_t)p);
                    }
                }
                w &= (w - 1);
            }
        }
    }
}

static int run_multi_bitmap_near(
    near_multi_ctx *st,
    int want_positions,
    unsigned char **out_blob,
    int *out_len
) {
    *out_blob = NULL;
    *out_len = 0;
    if (!st || st->groups_cap <= 1 || st->max_grp < 2) return 1;
    if (st->count[1] <= 0) return 1;

    int groups_n = 0;
    for (int g = 1; g <= st->max_grp; g++) {
        if (g < st->groups_cap && st->count[g] > 0) groups_n++;
    }
    if (groups_n < 2) return 1;

    int chunk_size = st->chunk_size > 0 ? st->chunk_size : 4096;
    if (chunk_size % 64 != 0) chunk_size = ((chunk_size + 63) / 64) * 64;
    int words = chunk_size / 64;

    blob_iter *anchor_iters = sqlite3_malloc(sizeof(*anchor_iters) * st->count[1]);
    if (!anchor_iters) return 0;
    for (int i = 0; i < st->count[1]; i++) {
        anchor_iters[i].ptr = st->blobs[1][i];
        anchor_iters[i].end = st->blobs[1][i] + st->lens[1][i];
        anchor_iters[i].acc = 0;
        anchor_iters[i].active = next_seq(&anchor_iters[i].ptr, anchor_iters[i].end, &anchor_iters[i].acc);
    }
    uint64_t a_pos = 0;
    int has_a = group_next(anchor_iters, st->count[1], &a_pos);
    if (!has_a) {
        sqlite3_free(anchor_iters);
        return 1;
    }

    multi_group_state *states = sqlite3_malloc(sizeof(*states) * (st->max_grp + 1));
    if (!states) {
        sqlite3_free(anchor_iters);
        return 0;
    }
    memset(states, 0, sizeof(*states) * (st->max_grp + 1));
    int64_t anchor_idx = (int64_t)(a_pos / (uint64_t)chunk_size);
    for (int g = 1; g <= st->max_grp; g++) {
        if (g >= st->groups_cap || st->count[g] <= 0) continue;
        if (!init_multi_group_state(&states[g], st, g, words, chunk_size, anchor_idx)) {
            for (int k = 1; k <= st->max_grp; k++) {
                if (k < st->groups_cap && st->count[k] > 0) free_multi_group_state(&states[k]);
            }
            sqlite3_free(states);
            sqlite3_free(anchor_iters);
            return 0;
        }
    }

    uint64_t *combined = sqlite3_malloc(sizeof(uint64_t) * words);
    uint64_t *hits = sqlite3_malloc(sizeof(uint64_t) * words);
    if (!combined || !hits) {
        sqlite3_free(combined);
        sqlite3_free(hits);
        for (int k = 1; k <= st->max_grp; k++) {
            if (k < st->groups_cap && st->count[k] > 0) free_multi_group_state(&states[k]);
        }
        sqlite3_free(states);
        sqlite3_free(anchor_iters);
        return 0;
    }

    unsigned char *out = NULL;
    int out_len_local = 0;
    int out_cap = 0;
    uint64_t last_out = 0;
    int has_out = 0;
    int count = 0;

    int64_t chunk_idx = anchor_idx;
    while (1) {
        for (int g = 1; g <= st->max_grp; g++) {
            if (g < st->groups_cap && st->count[g] > 0) {
                rotate_multi_group_state(&states[g], chunk_idx, words, chunk_size);
            }
        }
        multi_group_state *anchor = &states[1];
        if (!any_bits_set(anchor->bits_curr, words)) {
            if (!anchor->has_b && !any_bits_set(anchor->bits_next, words)) break;
            chunk_idx++;
            continue;
        }

        memcpy(combined, anchor->bits_curr, sizeof(uint64_t) * words);
        for (int g = 2; g <= st->max_grp; g++) {
            if (g >= st->groups_cap || st->count[g] <= 0) continue;
            build_anchor_hit_mask_for_group(&states[g], words, chunk_size, st->off_min, st->off_max, hits);
            for (int wi = 0; wi < words; wi++) combined[wi] &= hits[wi];
        }

        if (any_bits_set(combined, words)) {
            if (!want_positions) {
                for (int wi = 0; wi < words; wi++) {
                    count += __builtin_popcountll(combined[wi]);
                }
            } else {
                uint64_t chunk_start = (uint64_t)chunk_idx * (uint64_t)chunk_size;
                for (int wi = 0; wi < words; wi++) {
                    uint64_t w = combined[wi];
                    while (w) {
                        int bit = __builtin_ctzll(w);
                        uint64_t pos = chunk_start + ((uint64_t)wi << 6) + (uint64_t)bit;
                        uint64_t delta = has_out ? (pos - last_out) : pos;
                        if (!append_varint_bytes(delta, &out, &out_len_local, &out_cap)) {
                            sqlite3_free(out);
                            sqlite3_free(combined);
                            sqlite3_free(hits);
                            for (int k = 1; k <= st->max_grp; k++) {
                                if (k < st->groups_cap && st->count[k] > 0) free_multi_group_state(&states[k]);
                            }
                            sqlite3_free(states);
                            sqlite3_free(anchor_iters);
                            return 0;
                        }
                        count++;
                        last_out = pos;
                        has_out = 1;
                        w &= (w - 1);
                    }
                }
            }
        }
        chunk_idx++;
    }

    sqlite3_free(combined);
    sqlite3_free(hits);
    for (int k = 1; k <= st->max_grp; k++) {
        if (k < st->groups_cap && st->count[k] > 0) free_multi_group_state(&states[k]);
    }
    sqlite3_free(states);
    sqlite3_free(anchor_iters);
    if (!want_positions) {
        *out_blob = NULL;
        *out_len = count;
    } else {
        *out_blob = out;
        *out_len = out_len_local;
    }
    return 1;
}

static void post_near_count_bitmap_multi_groups_step(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 5) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_multi_groups(grp, blob, off_min, off_max, chunk_size) expects 5 args", -1);
        return;
    }
    int grp = sqlite3_value_int(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);
    int chunk_size = sqlite3_value_int(argv[4]);
    if (grp < 1 || !b || b_len <= 0) return;

    near_multi_ctx *st = sqlite3_aggregate_context(ctx, sizeof(*st));
    if (!st) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_multi_groups: OOM", -1);
        return;
    }
    if (!st->has_offsets) {
        st->off_min = off_min;
        st->off_max = off_max;
        st->chunk_size = chunk_size;
        st->has_offsets = 1;
    }
    if (!ensure_multi_group_capacity(st, grp)) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_multi_groups: OOM", -1);
        return;
    }
    if (!append_blob_group(&st->blobs[grp], &st->lens[grp], &st->count[grp], &st->cap[grp], b, b_len)) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_multi_groups: OOM", -1);
        return;
    }
    if (grp > st->max_grp) st->max_grp = grp;
}

static void post_near_positions_bitmap_multi_groups_step(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    post_near_count_bitmap_multi_groups_step(ctx, argc, argv);
}

static void post_near_count_bitmap_multi_groups_final(sqlite3_context *ctx) {
    near_multi_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || st->max_grp < 2 || !st->count || st->count[1] <= 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }
    unsigned char *out = NULL;
    int out_len = 0;
    if (!run_multi_bitmap_near(st, 0, &out, &out_len)) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_multi_groups: OOM", -1);
        return;
    }
    sqlite3_result_int(ctx, out_len);
    free_multi_groups(st);
    sqlite3_free(st->blobs);
    sqlite3_free(st->lens);
    sqlite3_free(st->count);
    sqlite3_free(st->cap);
    st->blobs = NULL;
    st->lens = NULL;
    st->count = NULL;
    st->cap = NULL;
    st->groups_cap = 0;
    st->max_grp = 0;
}

static void post_near_positions_bitmap_multi_groups_final(sqlite3_context *ctx) {
    near_multi_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || st->max_grp < 2 || !st->count || st->count[1] <= 0) {
        sqlite3_result_blob(ctx, "", 0, SQLITE_STATIC);
        return;
    }
    unsigned char *out = NULL;
    int out_len = 0;
    if (!run_multi_bitmap_near(st, 1, &out, &out_len)) {
        sqlite3_result_error(ctx, "post_near_positions_bitmap_multi_groups: OOM", -1);
        return;
    }
    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
    free_multi_groups(st);
    sqlite3_free(st->blobs);
    sqlite3_free(st->lens);
    sqlite3_free(st->count);
    sqlite3_free(st->cap);
    st->blobs = NULL;
    st->lens = NULL;
    st->count = NULL;
    st->cap = NULL;
    st->groups_cap = 0;
    st->max_grp = 0;
}

static void post_near_count_bitmap_groups_final(sqlite3_context *ctx) {
    near_groups_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || st->count1 == 0 || st->count2 == 0) {
        sqlite3_result_int(ctx, 0);
        return;
    }
    unsigned char *out = NULL;
    int out_len = 0;
    if (!run_bitmap_near(st, 0, &out, &out_len)) {
        sqlite3_result_error(ctx, "post_near_count_bitmap_groups: OOM", -1);
        return;
    }
    sqlite3_result_int(ctx, out_len);
    free_group_arrays(st->blobs1, st->count1, st->lens1);
    free_group_arrays(st->blobs2, st->count2, st->lens2);
    st->blobs1 = NULL;
    st->blobs2 = NULL;
    st->count1 = st->count2 = 0;
    st->cap1 = st->cap2 = 0;
}

static void post_near_positions_bitmap_groups_step(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    post_near_count_bitmap_groups_step(ctx, argc, argv);
}

static void post_near_positions_bitmap_groups_final(sqlite3_context *ctx) {
    near_groups_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || st->count1 == 0 || st->count2 == 0) {
        sqlite3_result_blob(ctx, "", 0, SQLITE_STATIC);
        return;
    }
    unsigned char *out = NULL;
    int out_len = 0;
    if (!run_bitmap_near(st, 1, &out, &out_len)) {
        sqlite3_result_error(ctx, "post_near_positions_bitmap_groups: OOM", -1);
        return;
    }
    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
    free_group_arrays(st->blobs1, st->count1, st->lens1);
    free_group_arrays(st->blobs2, st->count2, st->lens2);
    st->blobs1 = NULL;
    st->blobs2 = NULL;
    st->count1 = st->count2 = 0;
    st->cap1 = st->cap2 = 0;
}

static void post_near_positions_groups_final(sqlite3_context *ctx) {
    near_groups_ctx *st = sqlite3_aggregate_context(ctx, 0);
    if (!st || st->count1 == 0 || st->count2 == 0) {
        sqlite3_result_blob(ctx, "", 0, SQLITE_STATIC);
        return;
    }

    blob_iter *iters1 = sqlite3_malloc(sizeof(*iters1) * st->count1);
    blob_iter *iters2 = sqlite3_malloc(sizeof(*iters2) * st->count2);
    if (!iters1 || !iters2) {
        sqlite3_free(iters1);
        sqlite3_free(iters2);
        sqlite3_result_error(ctx, "post_near_positions_groups: OOM", -1);
        return;
    }
    for (int i = 0; i < st->count1; i++) {
        iters1[i].ptr = st->blobs1[i];
        iters1[i].end = st->blobs1[i] + st->lens1[i];
        iters1[i].acc = 0;
        iters1[i].active = next_seq(&iters1[i].ptr, iters1[i].end, &iters1[i].acc);
    }
    for (int i = 0; i < st->count2; i++) {
        iters2[i].ptr = st->blobs2[i];
        iters2[i].end = st->blobs2[i] + st->lens2[i];
        iters2[i].acc = 0;
        iters2[i].active = next_seq(&iters2[i].ptr, iters2[i].end, &iters2[i].acc);
    }

    uint64_t a_val = 0, b_val = 0;
    int has_a = group_next(iters1, st->count1, &a_val);
    int has_b = group_next(iters2, st->count2, &b_val);

    unsigned char *out = NULL;
    int out_len = 0;
    int out_cap = 0;
    uint64_t last_out = 0;
    int has_out = 0;

    while (has_a && has_b) {
        int64_t diff = (int64_t)b_val - (int64_t)a_val;
        if (diff < st->off_min) {
            has_b = group_next(iters2, st->count2, &b_val);
        } else if (diff > st->off_max) {
            has_a = group_next(iters1, st->count1, &a_val);
        } else {
            uint64_t delta = has_out ? (a_val - last_out) : a_val;
            if (!append_varint_bytes(delta, &out, &out_len, &out_cap)) {
                sqlite3_free(out);
                sqlite3_free(iters1);
                sqlite3_free(iters2);
                free_group_arrays(st->blobs1, st->count1, st->lens1);
                free_group_arrays(st->blobs2, st->count2, st->lens2);
                st->blobs1 = NULL;
                st->lens1 = NULL;
                st->count1 = 0;
                st->cap1 = 0;
                st->blobs2 = NULL;
                st->lens2 = NULL;
                st->count2 = 0;
                st->cap2 = 0;
                sqlite3_result_error(ctx, "post_near_positions_groups: OOM", -1);
                return;
            }
            last_out = a_val;
            has_out = 1;
            has_a = group_next(iters1, st->count1, &a_val);
        }
    }

    sqlite3_free(iters1);
    sqlite3_free(iters2);
    free_group_arrays(st->blobs1, st->count1, st->lens1);
    free_group_arrays(st->blobs2, st->count2, st->lens2);
    st->blobs1 = NULL;
    st->lens1 = NULL;
    st->count1 = 0;
    st->cap1 = 0;
    st->blobs2 = NULL;
    st->lens2 = NULL;
    st->count2 = 0;
    st->cap2 = 0;

    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
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
 * post_near_positions_blob(blobA, blobB, off_min, off_max)
 *  - returnerer posisjoner i A der det finnes en B innenfor [off_min, off_max]
 *    som delta/varint-BLOB
 */
static void post_near_positions_blob_sqlite(
    sqlite3_context *ctx,
    int argc,
    sqlite3_value **argv
) {
    if (argc != 4) {
        sqlite3_result_error(ctx, "post_near_positions_blob(blob, blob, off_min, off_max) expects 4 args", -1);
        return;
    }

    const unsigned char *a = sqlite3_value_blob(argv[0]);
    const unsigned char *b = sqlite3_value_blob(argv[1]);
    int a_len = sqlite3_value_bytes(argv[0]);
    int b_len = sqlite3_value_bytes(argv[1]);
    int off_min = sqlite3_value_int(argv[2]);
    int off_max = sqlite3_value_int(argv[3]);

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
        int64_t diff = (int64_t)acc_b - (int64_t)acc_a;
        if (diff < off_min) {
            has_b = next_seq(&pb, eb, &acc_b);
        } else if (diff > off_max) {
            has_a = next_seq(&pa, ea, &acc_a);
        } else {
            uint64_t delta = acc_a - last_out;
            if (!append_varint_bytes(delta, &out, &out_len, &out_cap)) {
                sqlite3_free(out);
                sqlite3_result_error(ctx, "post_near_positions_blob: OOM", -1);
                return;
            }
            last_out = acc_a;
            has_a = next_seq(&pa, ea, &acc_a);
        }
    }

    sqlite3_result_blob(ctx, out, out_len, sqlite3_free);
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
        db, "post_near_positions_blob", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_near_positions_blob_sqlite, NULL, NULL
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
        db, "post_count", 1,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, post_count_sqlite, NULL, NULL
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_union_agg", 1,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_union_agg_step, post_union_agg_final
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_count_groups", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_near_count_groups_step, post_near_count_groups_final
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_positions_groups", 4,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_near_positions_groups_step, post_near_positions_groups_final
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_count_bitmap_groups", 5,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_near_count_bitmap_groups_step, post_near_count_bitmap_groups_final
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_positions_bitmap_groups", 5,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_near_positions_bitmap_groups_step, post_near_positions_bitmap_groups_final
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_count_bitmap_multi_groups", 5,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_near_count_bitmap_multi_groups_step, post_near_count_bitmap_multi_groups_final
    );
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(
        db, "post_near_positions_bitmap_multi_groups", 5,
        SQLITE_UTF8 | SQLITE_DETERMINISTIC,
        NULL, NULL, post_near_positions_bitmap_multi_groups_step, post_near_positions_bitmap_multi_groups_final
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
