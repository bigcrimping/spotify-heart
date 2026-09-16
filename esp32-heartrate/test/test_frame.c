/*
 * test_frame.c - host unit tests for mmwave_frame.c and mr60bha2.c.
 *
 * Build and run with test/run_tests.ps1 (TinyCC). The parser sources are
 * #included directly so a single translation unit is enough for `tcc -run`.
 *
 * Also replays any test/fixtures/*.txt captures (lines "frame: 01 00 ...")
 * through the parser when present; those files are produced in Phase 3.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../main/mmwave_frame.c"
#include "../main/mr60bha2.c"

static int g_checks = 0, g_fails = 0;

#define CHECK(cond)                                                          \
    do {                                                                     \
        g_checks++;                                                          \
        if (!(cond)) {                                                       \
            g_fails++;                                                       \
            printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
        }                                                                    \
    } while (0)

#define CHECK_EQ_U(a, b)                                                     \
    do {                                                                     \
        unsigned long _a = (unsigned long)(a), _b = (unsigned long)(b);      \
        g_checks++;                                                          \
        if (_a != _b) {                                                      \
            g_fails++;                                                       \
            printf("  FAIL %s:%d: %s == %lu, expected %s == %lu\n",          \
                   __FILE__, __LINE__, #a, _a, #b, _b);                      \
        }                                                                    \
    } while (0)

/* ------------------------------------------------------------------------ */
/* helpers                                                                   */

static void put_f32(uint8_t *b, float f)
{
    uint32_t u;
    memcpy(&u, &f, 4);
    b[0] = (uint8_t)u; b[1] = (uint8_t)(u >> 8);
    b[2] = (uint8_t)(u >> 16); b[3] = (uint8_t)(u >> 24);
}

static void put_u32(uint8_t *b, uint32_t u)
{
    b[0] = (uint8_t)u; b[1] = (uint8_t)(u >> 8);
    b[2] = (uint8_t)(u >> 16); b[3] = (uint8_t)(u >> 24);
}

static size_t build_hr(uint8_t *out, size_t cap, uint16_t id, float bpm)
{
    uint8_t d[4];
    put_f32(d, bpm);
    return mmwave_build_frame(out, cap, id, MR60_TYPE_HEART_RATE, d, 4);
}

/* Feed bytes; return number of frames emitted, last frame in *last. */
static int feed_all(mmwave_parser_t *p, const uint8_t *b, size_t n,
                    mmwave_frame_t *last)
{
    int frames = 0;
    for (size_t i = 0; i < n; i++) {
        if (mmwave_parser_feed(p, b[i], last)) {
            frames++;
        }
    }
    return frames;
}

/* Deterministic junk generator (LCG). */
static uint32_t g_seed = 12345;
static uint8_t junk_byte(void)
{
    g_seed = g_seed * 1103515245u + 12345u;
    return (uint8_t)(g_seed >> 16);
}

/* ------------------------------------------------------------------------ */
/* tests                                                                     */

static void test_checksum(void)
{
    puts("checksum");
    const uint8_t z[1] = {0};
    CHECK_EQ_U(mmwave_checksum(z, 0), 0xFF);                 /* LEN = 0 case */
    CHECK_EQ_U(mmwave_checksum(z, 1), 0xFF);
    const uint8_t a[3] = {0x01, 0x02, 0x04};
    CHECK_EQ_U(mmwave_checksum(a, 3), (uint8_t)~0x07);
    /* Known header: 01 00 01 00 04 0A 15 -> xor = 0x1B, cksum = 0xE4 */
    const uint8_t h[7] = {0x01, 0x00, 0x01, 0x00, 0x04, 0x0A, 0x15};
    CHECK_EQ_U(mmwave_checksum(h, 7), 0xE4);
}

static void test_clean_frame(void)
{
    puts("clean heart-rate frame, byte by byte");
    uint8_t f[32];
    size_t n = build_hr(f, sizeof f, 0x0001, 72.5f);
    CHECK_EQ_U(n, 13);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = 0;
    for (size_t i = 0; i < n; i++) {
        bool got = mmwave_parser_feed(&p, f[i], &out);
        if (i + 1 < n) CHECK(!got);          /* nothing before the last byte */
        if (got) frames++;
    }
    CHECK_EQ_U(frames, 1);
    CHECK_EQ_U(out.type, MR60_TYPE_HEART_RATE);
    CHECK_EQ_U(out.id, 1);
    CHECK_EQ_U(out.len, 4);
    CHECK_EQ_U(out.raw_len, 13);
    CHECK(memcmp(out.raw, f, 13) == 0);
    CHECK_EQ_U(p.pos, 0);
    CHECK_EQ_U(p.stats.frames_ok, 1);
    CHECK_EQ_U(p.stats.resyncs, 0);

    mr60_reading_t r;
    CHECK(mr60bha2_decode(&out, &r));
    CHECK(r.type == MR60_TYPE_HEART_RATE);
    CHECK(r.u.heart_rate == 72.5f);
}

static void test_leading_junk(void)
{
    puts("junk (with stray SOF bytes) before a frame");
    uint8_t buf[64];
    size_t n = 0;
    /* 20 junk bytes with stray SOFs at 3, 9 and 16. Filler is 0xEE so every
     * stray SOF is followed by an impossible LEN (>= 0xEE01) and is rejected
     * as soon as its 8-byte pseudo-header is in. (A stray SOF whose LEN bytes
     * happen to look plausible is the case the idle reset covers; see
     * test_idle_reset.) */
    for (int i = 0; i < 20; i++) buf[n++] = 0xEE;
    buf[3] = MMWAVE_SOF;
    buf[9] = MMWAVE_SOF;
    buf[16] = MMWAVE_SOF;
    n += build_hr(buf + n, sizeof buf - n, 0x0002, 61.0f);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = feed_all(&p, buf, n, &out);
    CHECK_EQ_U(frames, 1);
    CHECK_EQ_U(out.id, 2);
    mr60_reading_t r;
    CHECK(mr60bha2_decode(&out, &r) && r.u.heart_rate == 61.0f);
    CHECK(p.stats.resyncs > 0);              /* a stray SOF was rejected */
    CHECK_EQ_U(p.pos, 0);
}

static void test_back_to_back(void)
{
    puts("two frames back to back");
    uint8_t buf[64];
    size_t n = build_hr(buf, sizeof buf, 10, 70.0f);
    n += build_hr(buf + n, sizeof buf - n, 11, 71.0f);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = 0;
    uint16_t ids[2] = {0, 0};
    for (size_t i = 0; i < n; i++) {
        if (mmwave_parser_feed(&p, buf[i], &out)) {
            if (frames < 2) ids[frames] = out.id;
            frames++;
        }
    }
    CHECK_EQ_U(frames, 2);
    CHECK_EQ_U(ids[0], 10);
    CHECK_EQ_U(ids[1], 11);
    CHECK_EQ_U(p.stats.resyncs, 0);
}

static void test_corrupt_data_then_good(void)
{
    puts("corrupted data byte, then a good frame");
    uint8_t buf[64];
    size_t n = build_hr(buf, sizeof buf, 20, 80.0f);
    buf[9] ^= 0x40;                          /* flip a payload bit */
    n += build_hr(buf + n, sizeof buf - n, 21, 81.0f);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = feed_all(&p, buf, n, &out);
    CHECK_EQ_U(frames, 1);
    CHECK_EQ_U(out.id, 21);
    CHECK_EQ_U(p.stats.bad_data_cksum, 1);
    CHECK_EQ_U(p.stats.bad_head_cksum, 0);
    CHECK_EQ_U(p.stats.frames_ok, 1);
}

static void test_corrupt_header_then_good(void)
{
    puts("corrupted header byte, then a good frame");
    uint8_t buf[64];
    size_t n = build_hr(buf, sizeof buf, 30, 80.0f);
    buf[6] ^= 0x01;                          /* TYPE low byte */
    n += build_hr(buf + n, sizeof buf - n, 31, 81.0f);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = feed_all(&p, buf, n, &out);
    CHECK_EQ_U(frames, 1);
    CHECK_EQ_U(out.id, 31);
    CHECK_EQ_U(p.stats.bad_head_cksum, 1);
}

static void test_oversize_then_good(void)
{
    puts("header claiming LEN = 600, then a good frame");
    uint8_t buf[64];
    size_t n = 0;
    /* hand-built bogus header: SOF, ID, LEN=600 (0x0258), TYPE, cksum */
    buf[n++] = MMWAVE_SOF; buf[n++] = 0; buf[n++] = 1;
    buf[n++] = 0x02; buf[n++] = 0x58;
    buf[n++] = 0x0A; buf[n++] = 0x15;
    buf[n] = mmwave_checksum(buf, 7); n++;
    n += build_hr(buf + n, sizeof buf - n, 40, 90.0f);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = feed_all(&p, buf, n, &out);
    CHECK_EQ_U(frames, 1);
    CHECK_EQ_U(out.id, 40);
    /* At least one oversize rejection, but not exactly one: the resync shifts
     * to the 0x01 inside the bogus header, and those bytes happen to form a
     * second valid-looking header claiming LEN = 0x0A15. Counting it again is
     * correct; the frame behind it is still recovered. */
    CHECK(p.stats.oversize >= 1);
}

static void test_zero_length_frame(void)
{
    puts("LEN = 0 frame with trailing 0xFF checksum");
    uint8_t buf[16];
    size_t n = mmwave_build_frame(buf, sizeof buf, 50, 0x1234, NULL, 0);
    CHECK_EQ_U(n, 9);
    CHECK_EQ_U(buf[8], 0xFF);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = feed_all(&p, buf, n, &out);
    CHECK_EQ_U(frames, 1);
    CHECK_EQ_U(out.len, 0);
    CHECK_EQ_U(out.type, 0x1234);
    mr60_reading_t r;
    CHECK(!mr60bha2_decode(&out, &r));       /* unknown type */
}

static void test_max_length_frame(void)
{
    puts("LEN = 512 frame (largest allowed)");
    uint8_t data[MMWAVE_MAX_DATA_LEN];
    for (int i = 0; i < MMWAVE_MAX_DATA_LEN; i++) data[i] = (uint8_t)i;
    uint8_t buf[MMWAVE_MAX_FRAME_LEN + 16];
    size_t n = mmwave_build_frame(buf, sizeof buf, 60, 0x0A08, data,
                                  MMWAVE_MAX_DATA_LEN);
    CHECK_EQ_U(n, MMWAVE_MAX_FRAME_LEN);
    n += build_hr(buf + n, sizeof buf - n, 61, 55.0f);

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = 0;
    uint16_t first_id = 0;
    for (size_t i = 0; i < n; i++) {
        if (mmwave_parser_feed(&p, buf[i], &out)) {
            if (frames == 0) first_id = out.id;
            frames++;
        }
    }
    CHECK_EQ_U(frames, 2);
    CHECK_EQ_U(first_id, 60);
    CHECK_EQ_U(out.id, 61);
    CHECK_EQ_U(mmwave_build_frame(buf, sizeof buf, 0, 0, data, 513), 0);
}

static void test_false_frame_wrapping_real_one(void)
{
    puts("bogus header claiming LEN = 20, then real frames");
    /* The bogus header's checksum is wrong, so it is rejected as soon as its
     * 8th byte lands, long before its claimed 20-byte payload could swallow
     * the real frames behind it. Both must come through untouched. */
    uint8_t buf[80];
    size_t n = 0;
    buf[n++] = MMWAVE_SOF; buf[n++] = 0; buf[n++] = 9;
    buf[n++] = 0x00; buf[n++] = 20;
    buf[n++] = 0x0A; buf[n++] = 0x15;
    buf[n++] = 0x00;                         /* wrong header checksum */
    n += build_hr(buf + n, sizeof buf - n, 70, 65.0f);   /* bytes 8..20 */
    n += build_hr(buf + n, sizeof buf - n, 71, 66.0f);   /* bytes 21..33 */

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = 0;
    uint16_t ids[2] = {0, 0};
    for (size_t i = 0; i < n; i++) {
        if (mmwave_parser_feed(&p, buf[i], &out)) {
            if (frames < 2) ids[frames] = out.id;
            frames++;
        }
    }
    CHECK_EQ_U(frames, 2);
    CHECK_EQ_U(ids[0], 70);
    CHECK_EQ_U(ids[1], 71);
    CHECK_EQ_U(p.stats.bad_head_cksum, 1);
    /* No SOF hides inside the rejected header, so the buffer is simply
     * dropped: the recovery costs 8 bytes and no real frame is lost. */
    CHECK_EQ_U(p.stats.resyncs, 0);
    CHECK_EQ_U(p.pos, 0);
}

static void test_false_sof_does_not_swallow_stream(void)
{
    puts("false SOF mid-stream does not swallow the frames behind it");
    /* Regression test for the header-checksum ordering. A false SOF claiming
     * a 200-byte payload used to make the parser buffer 209 bytes before it
     * looked at the checksum, eating the ~15 real frames that arrived in the
     * meantime. The header is now checked at byte 8, so every real frame
     * behind it survives. */
    uint8_t buf[256];
    size_t  n = 0;
    buf[n++] = MMWAVE_SOF; buf[n++] = 0; buf[n++] = 1;
    buf[n++] = 0x00; buf[n++] = 200;         /* claims a 200-byte payload */
    buf[n++] = 0x0A; buf[n++] = 0x15;
    buf[n++] = 0x00;                         /* wrong header checksum */
    for (int i = 0; i < 5; i++) {
        n += build_hr(buf + n, sizeof buf - n, (uint16_t)(100 + i), 70.0f + i);
    }

    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int      frames = 0;
    uint16_t ids[5] = {0};
    for (size_t i = 0; i < n; i++) {
        if (mmwave_parser_feed(&p, buf[i], &out)) {
            if (frames < 5) ids[frames] = out.id;
            frames++;
        }
    }
    CHECK_EQ_U(frames, 5);                   /* nothing lost */
    for (int i = 0; i < 5; i++) {
        CHECK_EQ_U(ids[i], (unsigned)(100 + i));
    }
    CHECK(p.stats.bad_head_cksum >= 1);
    CHECK_EQ_U(p.pos, 0);
}

static void test_random_noise_no_crash(void)
{
    puts("100 kB of random noise, then a good frame");
    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    int frames = 0;
    for (int i = 0; i < 100000; i++) {
        if (mmwave_parser_feed(&p, junk_byte(), &out)) frames++;
    }
    /* A valid frame by chance needs two matching checksums: essentially
     * never in 100 kB. Accept 0 or a tiny number, but never a crash. */
    CHECK(frames <= 2);
    CHECK(p.stats.oversize + p.stats.bad_head_cksum + p.stats.bad_data_cksum > 100);
    uint8_t f[32];
    size_t n = build_hr(f, sizeof f, 80, 99.0f);
    /* The parser may be mid-bogus-frame. The radar task resets it after an
     * idle gap, so do the same here: the next real frame must come through
     * on its own. */
    mmwave_parser_reset(&p);
    CHECK_EQ_U(feed_all(&p, f, n, &out), 1);
    CHECK_EQ_U(out.id, 80);
    CHECK(p.stats.frames_ok >= 1);          /* stats survive a reset */
}

static void test_idle_reset(void)
{
    puts("stray SOF with plausible LEN, idle reset, then a good frame");
    /* The one case the header checksum cannot catch: a stray SOF whose eight
     * bytes happen to form a *valid* header claiming LEN = 256, so the parser
     * legitimately waits for 265 bytes. An idle gap means that frame will
     * never complete; the reset must drop it without losing the next one. */
    uint8_t stray[8] = {MMWAVE_SOF, 0x00, 0x05, 0x01, 0x00, 0x0A, 0x15, 0x00};
    stray[7] = mmwave_checksum(stray, MMWAVE_HEADER_LEN - 1);
    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    CHECK_EQ_U(feed_all(&p, stray, sizeof stray, &out), 0);
    CHECK_EQ_U(p.pos, 8);                    /* waiting for payload */

    /* Without a reset, a real frame just gets swallowed. */
    uint8_t f[32];
    size_t n = build_hr(f, sizeof f, 90, 64.0f);
    CHECK_EQ_U(feed_all(&p, f, n, &out), 0);
    CHECK_EQ_U(p.pos, 8 + n);

    /* With the reset, it parses. */
    mmwave_parser_reset(&p);
    CHECK_EQ_U(p.pos, 0);
    CHECK_EQ_U(feed_all(&p, f, n, &out), 1);
    CHECK_EQ_U(out.id, 90);
    CHECK_EQ_U(p.stats.frames_ok, 1);
}

static void test_decode_other_types(void)
{
    puts("decode breath, distance, phases, presence, firmware, targets");
    uint8_t d[64], f[128];
    mmwave_frame_t fr;
    mr60_reading_t r;
    mmwave_parser_t p;
    mmwave_parser_init(&p);

    put_f32(d, 15.25f);
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 1, MR60_TYPE_BREATH_RATE, d, 4), &fr);
    CHECK(mr60bha2_decode(&fr, &r) && r.type == MR60_TYPE_BREATH_RATE && r.u.breath_rate == 15.25f);

    put_u32(d, 1); put_f32(d + 4, 86.4f);
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 2, MR60_TYPE_DISTANCE, d, 8), &fr);
    CHECK(mr60bha2_decode(&fr, &r) && r.u.distance.flag == 1 && r.u.distance.range == 86.4f);

    put_f32(d, 1.0f); put_f32(d + 4, 2.0f); put_f32(d + 8, 3.0f);
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 3, MR60_TYPE_PHASES, d, 12), &fr);
    CHECK(mr60bha2_decode(&fr, &r) && r.u.phases.total == 1.0f &&
          r.u.phases.breath == 2.0f && r.u.phases.heart == 3.0f);

    d[0] = 1;
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 4, MR60_TYPE_PRESENCE, d, 1), &fr);
    CHECK(mr60bha2_decode(&fr, &r) && r.u.presence == 1);

    d[0] = 7; d[1] = 1; d[2] = 2; d[3] = 3;
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 5, MR60_TYPE_FIRMWARE, d, 4), &fr);
    CHECK(mr60bha2_decode(&fr, &r) && r.u.firmware.project == 7 &&
          r.u.firmware.major == 1 && r.u.firmware.sub == 2 && r.u.firmware.modified == 3);

    /* point cloud with n = 3 */
    put_u32(d, 3);
    for (int i = 0; i < 3; i++) {
        uint8_t *t = d + 4 + i * 16;
        put_f32(t, 0.5f * i); put_f32(t + 4, -1.0f * i);
        put_u32(t + 8, (uint32_t)(int32_t)(-i)); put_u32(t + 12, (uint32_t)i);
    }
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 6, MR60_TYPE_POINT_CLOUD, d, 4 + 3 * 16), &fr);
    CHECK(mr60bha2_decode(&fr, &r));
    CHECK(r.type == MR60_TYPE_POINT_CLOUD);
    CHECK(r.type != MR60_TYPE_HEART_RATE);
    CHECK_EQ_U(r.u.targets.count, 3);
    CHECK_EQ_U(r.u.targets.stored, 3);
    CHECK(r.u.targets.t[2].x == 1.0f && r.u.targets.t[2].y == -2.0f);
    CHECK(r.u.targets.t[2].dop_index == -2 && r.u.targets.t[2].cluster_index == 2);

    /* short payloads are rejected */
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 7, MR60_TYPE_HEART_RATE, d, 3), &fr);
    CHECK(!mr60bha2_decode(&fr, &r));
    put_u32(d, 3);
    feed_all(&p, f, mmwave_build_frame(f, sizeof f, 8, MR60_TYPE_POINT_CLOUD, d, 4 + 2 * 16), &fr);
    CHECK(!mr60bha2_decode(&fr, &r));
}

/* ------------------------------------------------------------------------ */
/* fixture replay (optional; files appear in Phase 3)                        */

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void replay_fixture(const char *path)
{
    FILE *fp = fopen(path, "r");
    if (!fp) return;
    printf("fixture %s\n", path);
    mmwave_parser_t p;
    mmwave_parser_init(&p);
    mmwave_frame_t out;
    mr60_reading_t r;
    int hr_frames = 0, frames = 0;
    char line[4096];
    while (fgets(line, sizeof line, fp)) {
        const char *s = line;
        if (strncmp(s, "frame:", 6) != 0) continue;
        s += 6;
        while (*s) {
            while (*s == ' ' || *s == '\t') s++;
            int hi = hexval(s[0]), lo = hexval(s[1]);
            if (hi < 0 || lo < 0) break;
            if (mmwave_parser_feed(&p, (uint8_t)(hi << 4 | lo), &out)) {
                frames++;
                if (mr60bha2_decode(&out, &r) && r.type == MR60_TYPE_HEART_RATE) {
                    hr_frames++;
                    /* 0.00 is what the radar sends at 10 Hz when nobody is
                     * in front of it (capture_nobody_present.txt). */
                    CHECK(r.u.heart_rate >= 0.0f && r.u.heart_rate < 250.0f);
                }
            }
            s += 2;
        }
    }
    fclose(fp);
    printf("  frames=%d heart_rate=%d bad_head=%lu bad_data=%lu oversize=%lu resyncs=%lu\n",
           frames, hr_frames, (unsigned long)p.stats.bad_head_cksum,
           (unsigned long)p.stats.bad_data_cksum, (unsigned long)p.stats.oversize,
           (unsigned long)p.stats.resyncs);
    CHECK(frames > 0);
    CHECK(hr_frames > 0);
    CHECK_EQ_U(p.stats.bad_head_cksum, 0);
    CHECK_EQ_U(p.stats.bad_data_cksum, 0);
}

int main(int argc, char **argv)
{
    test_checksum();
    test_clean_frame();
    test_leading_junk();
    test_back_to_back();
    test_corrupt_data_then_good();
    test_corrupt_header_then_good();
    test_oversize_then_good();
    test_zero_length_frame();
    test_max_length_frame();
    test_false_frame_wrapping_real_one();
    test_false_sof_does_not_swallow_stream();
    test_random_noise_no_crash();
    test_idle_reset();
    test_decode_other_types();

    for (int i = 1; i < argc; i++) {
        replay_fixture(argv[i]);
    }

    printf("\n%d checks, %d failures: %s\n", g_checks, g_fails,
           g_fails ? "FAIL" : "ALL PASS");
    return g_fails ? 1 : 0;
}
