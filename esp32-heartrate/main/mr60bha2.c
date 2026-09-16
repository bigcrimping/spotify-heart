/* mr60bha2.c - see mr60bha2.h. Pure C99. */
#include "mr60bha2.h"

#include <string.h>

static uint32_t rd_u32(const uint8_t *b)
{
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) |
           ((uint32_t)b[3] << 24);
}

static int32_t rd_i32(const uint8_t *b)
{
    return (int32_t)rd_u32(b);
}

static float rd_f32(const uint8_t *b)
{
    uint32_t u = rd_u32(b);
    float f;
    memcpy(&f, &u, sizeof f);
    return f;
}

static bool decode_targets(const uint8_t *d, uint16_t len, mr60_reading_t *out)
{
    if (len < 4) {
        return false;
    }
    uint32_t n = rd_u32(d);
    if ((uint32_t)len < 4u + n * 16u) {
        return false;
    }
    out->u.targets.count  = n;
    out->u.targets.stored = n < MR60_MAX_TARGETS ? n : MR60_MAX_TARGETS;
    d += 4;
    for (uint32_t i = 0; i < out->u.targets.stored; i++, d += 16) {
        out->u.targets.t[i].x             = rd_f32(d);
        out->u.targets.t[i].y             = rd_f32(d + 4);
        out->u.targets.t[i].dop_index     = rd_i32(d + 8);
        out->u.targets.t[i].cluster_index = rd_i32(d + 12);
    }
    return true;
}

bool mr60bha2_decode(const mmwave_frame_t *f, mr60_reading_t *out)
{
    const uint8_t *d   = mmwave_frame_data(f);
    uint16_t       len = f->len;

    out->type = (mr60_type_t)f->type;
    switch (f->type) {
    case MR60_TYPE_HEART_RATE:
        if (len < 4) return false;
        out->u.heart_rate = rd_f32(d);
        return true;
    case MR60_TYPE_BREATH_RATE:
        if (len < 4) return false;
        out->u.breath_rate = rd_f32(d);
        return true;
    case MR60_TYPE_DISTANCE:
        if (len < 8) return false;
        out->u.distance.flag  = rd_u32(d);
        out->u.distance.range = rd_f32(d + 4);
        return true;
    case MR60_TYPE_PHASES:
        if (len < 12) return false;
        out->u.phases.total  = rd_f32(d);
        out->u.phases.breath = rd_f32(d + 4);
        out->u.phases.heart  = rd_f32(d + 8);
        return true;
    case MR60_TYPE_PRESENCE:
        if (len < 1) return false;
        out->u.presence = d[0];
        return true;
    case MR60_TYPE_POINT_CLOUD:
    case MR60_TYPE_TARGET_INFO:
        return decode_targets(d, len, out);
    case MR60_TYPE_FIRMWARE:
        if (len < 4) return false;
        out->u.firmware.project  = d[0];
        out->u.firmware.major    = d[1];
        out->u.firmware.sub      = d[2];
        out->u.firmware.modified = d[3];
        return true;
    default:
        return false;
    }
}
