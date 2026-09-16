/* radar_task.h - UART reader task that turns radar frames into output lines. */
#ifndef RADAR_TASK_H
#define RADAR_TASK_H

#include "esp_err.h"
#include "mmwave_frame.h"

/* Configure the radar UART and start the reader task. */
esp_err_t radar_task_start(void);

/* Snapshot of the parser counters (diagnostics only). */
void radar_task_get_stats(mmwave_stats_t *out);

#endif /* RADAR_TASK_H */
