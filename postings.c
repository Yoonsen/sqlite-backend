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
#define sqlite3_result_text       sqlite3_api->result_text
#define sqlite3_value_blob        sqlite3_api->value_blob
#define sqlite3_value_bytes       sqlite3_api->value_bytes
#define sqlite3_value_int         sqlite3_api->value_int
#define sqlite3_realloc           sqlite3_api->realloc
#define sqlite3_free              sqlite3_api->free
#endif

SQLITE_EXTENSION_INIT1

#include <stdint.h>
#include <stdio.h>
#include <string.h>

// Enkle varint/delta helpers – fyll ut senere
static uint64_t read_varint(const uint8_t **p, const uint8_t *end) {
    uint64_t x = 0;
    int shift = 0;
    while (*p < end) {
        uint8_t b = *(*p)++;
        x |= (uint64_t)(b & 0x7f) << shift;
        if ((b & 0x80) == 0) break;
        shift += 7;
    }
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

    return SQLITE_OK;
}
