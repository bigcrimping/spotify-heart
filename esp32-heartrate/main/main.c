/*
 * main.c - Spotify Heart radar firmware (variant B: board reports heart rate,
 * the Windows tray app does the Spotify work).
 *
 * Prints one "info:" line at boot, then hands over to radar_task, which
 * prints "heart_rate: %.2f" lines. The Windows app ignores the info line.
 */
#include <stdio.h>

#include "esp_app_desc.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_system.h"

#include "radar_task.h"

static const char *TAG = "main";

void app_main(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    printf("info: spotify-heart-hr %s esp32c6 reset=%d\n", app->version,
           (int)esp_reset_reason());
    fflush(stdout);

    esp_err_t err = radar_task_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "radar task failed to start: %s", esp_err_to_name(err));
        printf("error: radar task failed to start (%s)\n", esp_err_to_name(err));
        fflush(stdout);
    }
}
