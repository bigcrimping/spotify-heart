/*
 * mr60bha2.h - payload decoding for the Seeed MR60BHA2 breath/heart radar.
 *
 * Pure C99, no ESP-IDF. Type IDs and layouts come from the Arduino library
 * (Seeed-mmWave-library/src/SEEED_MR60BHA2.{h,cpp}); see PLAN.md section 1.
 * All multi-byte payload values are little-endian.
 */
#ifndef MR60BHA2_H
#define MR60BHA2_H

#include <stdbool.h>
#include <stdint.h>

#include "mmwave_frame.h"

typedef enum {
    MR60_TYPE_PHASES      = 0x0A13, /* 3 x float32: total, breath, heart      */
    MR60_TYPE_BREATH_RATE = 0x0A14, /* float32 breaths per minute             */
    MR60_TYPE_HEART_RATE  = 0x0A15, /* float32 beats per minute               */
    MR60_TYPE_DISTANCE    = 0x0A16, /* uint32 flag, float32 range             */
    MR60_TYPE_POINT_CLOUD = 0x0A08, /* uint32 n, n x target                   */
    MR60_TYPE_TARGET_INFO = 0x0A04, /* uint32 n, n x target                   */
    MR60_TYPE_PRESENCE    = 0x0F09, /* uint8: 0 nobody, 1 someone             */
    MR60_TYPE_FIRMWARE    = 0xFFFF, /* uint32: project, major, sub, modified  */
} mr60_type_t;

/* Targets kept per point-cloud/target-info frame. The sensor reports at most
 * 3 (MAX_TARGET_NUM in the Arduino header); extra ones are counted, not kept. */
#define MR60_MAX_TARGETS 4

typedef struct {
    float   x;
    float   y;
    int32_t dop_index;
    int32_t cluster_index;
} mr60_target_t;

typedef struct {
    mr60_type_t type;
    union {
        float heart_rate;
        float breath_rate;
        struct {
            uint32_t flag;      /* non-zero: range is valid */
            float    range;
        } distance;
        struct {
            float total;
            float breath;
            float heart;
        } phases;
        uint8_t presence;
        struct {
            uint32_t      count;    /* as reported by the sensor */
            uint32_t      stored;   /* min(count, MR60_MAX_TARGETS) */
            mr60_target_t t[MR60_MAX_TARGETS];
        } targets;
        struct {
            uint8_t project;
            uint8_t major;
            uint8_t sub;
            uint8_t modified;
        } firmware;
    } u;
} mr60_reading_t;

/*
 * Decode a verified frame. Returns false for unknown types or payloads that
 * are too short for their declared type; *out is then unspecified.
 */
bool mr60bha2_decode(const mmwave_frame_t *f, mr60_reading_t *out);

#endif /* MR60BHA2_H */
