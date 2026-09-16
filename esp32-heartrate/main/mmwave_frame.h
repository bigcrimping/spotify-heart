/*
 * mmwave_frame.h - byte-at-a-time parser for the Seeed mmWave serial framing.
 *
 * Pure C99. No ESP-IDF, no allocation, no I/O. The same file is compiled into
 * the firmware and into the host unit tests (test/test_frame.c).
 *
 * Frame layout (see PLAN.md section 1):
 *   [0]      SOF 0x01
 *   [1..2]   ID,   big-endian
 *   [3..4]   LEN,  big-endian, payload byte count (0..512)
 *   [5..6]   TYPE, big-endian
 *   [7]      HEAD_CKSUM = ~(xor of bytes 0..6)
 *   [8..]    DATA (LEN bytes, values inside are little-endian)
 *   [8+LEN]  DATA_CKSUM = ~(xor of DATA); 0xFF when LEN == 0
 */
#ifndef MMWAVE_FRAME_H
#define MMWAVE_FRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MMWAVE_SOF           0x01
#define MMWAVE_HEADER_LEN    8
#define MMWAVE_MAX_DATA_LEN  512
#define MMWAVE_MAX_FRAME_LEN (MMWAVE_HEADER_LEN + MMWAVE_MAX_DATA_LEN + 1)

/* A complete, checksum-verified frame. Self-contained copy; safe to keep. */
typedef struct {
    uint16_t id;
    uint16_t type;
    uint16_t len;                       /* payload length */
    size_t   raw_len;                   /* MMWAVE_HEADER_LEN + len + 1 */
    uint8_t  raw[MMWAVE_MAX_FRAME_LEN]; /* whole frame including checksums */
} mmwave_frame_t;

/* Payload pointer into a frame. */
static inline const uint8_t *mmwave_frame_data(const mmwave_frame_t *f)
{
    return f->raw + MMWAVE_HEADER_LEN;
}

typedef struct {
    uint32_t frames_ok;
    uint32_t bad_head_cksum;
    uint32_t bad_data_cksum;
    uint32_t oversize;      /* header claimed LEN > MMWAVE_MAX_DATA_LEN */
    uint32_t resyncs;       /* times the parser re-scanned discarded bytes */
} mmwave_stats_t;

typedef struct {
    uint8_t  buf[MMWAVE_MAX_FRAME_LEN];
    size_t   pos;           /* bytes held in buf; 0 == hunting for SOF */
    mmwave_stats_t stats;
} mmwave_parser_t;

void mmwave_parser_init(mmwave_parser_t *p);

/*
 * Discard any partially received frame but keep the statistics. Call this
 * when the line has been idle for longer than a frame could take (the radar
 * task does so after a 100 ms read timeout; a 13-byte frame lasts about 1 ms
 * at 115200 baud). Without it, noise that looks like a SOF plus a large LEN
 * would make the parser wait for hundreds of bytes and lose real frames.
 */
void mmwave_parser_reset(mmwave_parser_t *p);

/*
 * Feed one received byte. Returns true when a complete, verified frame has
 * been copied into *out. Bytes belonging to a following frame that were
 * already buffered are kept for the next call.
 */
bool mmwave_parser_feed(mmwave_parser_t *p, uint8_t byte, mmwave_frame_t *out);

/* ~(xor of data[0..len-1]) */
uint8_t mmwave_checksum(const uint8_t *data, size_t len);

/*
 * Build a frame into out (capacity cap). Always appends the data checksum,
 * even for len == 0, matching what the sensor sends. Returns the frame length,
 * or 0 if it does not fit or len > MMWAVE_MAX_DATA_LEN.
 */
size_t mmwave_build_frame(uint8_t *out, size_t cap, uint16_t id, uint16_t type,
                          const uint8_t *data, uint16_t len);

#endif /* MMWAVE_FRAME_H */
