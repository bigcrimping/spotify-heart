/* mmwave_frame.c - see mmwave_frame.h. Pure C99, no I/O, no allocation. */
#include "mmwave_frame.h"

#include <string.h>

uint8_t mmwave_checksum(const uint8_t *data, size_t len)
{
    uint8_t x = 0;
    for (size_t i = 0; i < len; i++) {
        x ^= data[i];
    }
    return (uint8_t)~x;
}

void mmwave_parser_init(mmwave_parser_t *p)
{
    memset(p, 0, sizeof *p);
}

void mmwave_parser_reset(mmwave_parser_t *p)
{
    p->pos = 0;
}

/*
 * Drop buf[0] and shift the buffer so it starts at the next SOF byte, if
 * any. Returns false when no SOF remains (buffer emptied).
 */
static bool shift_to_next_sof(mmwave_parser_t *p)
{
    for (size_t j = 1; j < p->pos; j++) {
        if (p->buf[j] == MMWAVE_SOF) {
            memmove(p->buf, p->buf + j, p->pos - j);
            p->pos -= j;
            p->stats.resyncs++;
            return true;
        }
    }
    p->pos = 0;
    return false;
}

bool mmwave_parser_feed(mmwave_parser_t *p, uint8_t byte, mmwave_frame_t *out)
{
    if (p->pos == 0) {
        if (byte != MMWAVE_SOF) {
            return false;               /* hunting: ignore noise */
        }
    }
    /* pos can never reach the buffer size: a frame completes at or before
     * MMWAVE_MAX_FRAME_LEN and oversize headers are rejected at byte 8. */
    p->buf[p->pos++] = byte;

    for (;;) {
        if (p->pos == 0) {
            return false;
        }
        if (p->buf[0] != MMWAVE_SOF) {
            /* Only reachable after a frame was emitted with trailing bytes. */
            if (!shift_to_next_sof(p)) {
                return false;
            }
            continue;
        }
        if (p->pos < MMWAVE_HEADER_LEN) {
            return false;               /* need more header bytes */
        }

        /* Verify the header before trusting anything in it, above all LEN.
         * Checking it here rather than after the payload means a false SOF
         * costs at most 8 buffered bytes instead of up to 520, which would
         * otherwise swallow the ~40 real frames that arrive meanwhile. */
        if (mmwave_checksum(p->buf, MMWAVE_HEADER_LEN - 1) != p->buf[7]) {
            p->stats.bad_head_cksum++;
            if (!shift_to_next_sof(p)) {
                return false;
            }
            continue;
        }

        uint16_t len = (uint16_t)((p->buf[3] << 8) | p->buf[4]);
        if (len > MMWAVE_MAX_DATA_LEN) {
            p->stats.oversize++;
            if (!shift_to_next_sof(p)) {
                return false;
            }
            continue;
        }

        size_t total = MMWAVE_HEADER_LEN + len + 1;
        if (p->pos < total) {
            return false;               /* need more payload bytes */
        }

        if (mmwave_checksum(p->buf + MMWAVE_HEADER_LEN, len) != p->buf[8 + len]) {
            p->stats.bad_data_cksum++;
            if (!shift_to_next_sof(p)) {
                return false;
            }
            continue;
        }

        /* Complete, verified frame. */
        out->id      = (uint16_t)((p->buf[1] << 8) | p->buf[2]);
        out->len     = len;
        out->type    = (uint16_t)((p->buf[5] << 8) | p->buf[6]);
        out->raw_len = total;
        memcpy(out->raw, p->buf, total);
        p->stats.frames_ok++;

        /* Keep any bytes that already belong to the next frame. */
        size_t rest = p->pos - total;
        if (rest) {
            memmove(p->buf, p->buf + total, rest);
        }
        p->pos = rest;
        return true;
    }
}

size_t mmwave_build_frame(uint8_t *out, size_t cap, uint16_t id, uint16_t type,
                          const uint8_t *data, uint16_t len)
{
    size_t total = MMWAVE_HEADER_LEN + len + 1;
    if (len > MMWAVE_MAX_DATA_LEN || cap < total) {
        return 0;
    }
    out[0] = MMWAVE_SOF;
    out[1] = (uint8_t)(id >> 8);
    out[2] = (uint8_t)(id & 0xFF);
    out[3] = (uint8_t)(len >> 8);
    out[4] = (uint8_t)(len & 0xFF);
    out[5] = (uint8_t)(type >> 8);
    out[6] = (uint8_t)(type & 0xFF);
    out[7] = mmwave_checksum(out, 7);
    if (len) {
        memcpy(out + MMWAVE_HEADER_LEN, data, len);
    }
    out[MMWAVE_HEADER_LEN + len] = mmwave_checksum(out + MMWAVE_HEADER_LEN, len);
    return total;
}
