/*
 * radar_task.c - reads the MR60BHA2 frame stream from UART1 and prints one
 * "heart_rate: %.2f" line per heart-rate frame on the USB console.
 *
 * Output contract (do not change without updating the Windows app):
 *   heart_rate: NN.NN\n     exactly once per received 0x0A15 frame
 * Anything else printed here must not start with "heart_rate:".
 */
#include "radar_task.h"

#include <stdio.h>

#include "driver/uart.h"
#include "hal/uart_ll.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "app_config.h"
#include "mmwave_frame.h"
#include "mr60bha2.h"

static mmwave_parser_t s_parser;

static void handle_frame(const mmwave_frame_t *f)
{
#if CONFIG_HR_DUMP_FRAMES
    fputs("frame:", stdout);
    for (size_t i = 0; i < f->raw_len; i++) {
        printf(" %02X", f->raw[i]);
    }
    putchar('\n');
#endif

    mr60_reading_t r;
    if (!mr60bha2_decode(f, &r)) {
        return;
    }

    switch (r.type) {
    case MR60_TYPE_HEART_RATE:
        printf("heart_rate: %.2f\n", r.u.heart_rate);
        break;
#if CONFIG_HR_PRINT_EXTRAS
    case MR60_TYPE_BREATH_RATE:
        printf("breath_rate: %.2f\n", r.u.breath_rate);
        break;
    case MR60_TYPE_DISTANCE:
        if (r.u.distance.flag) {
            printf("distance: %.2f\n", r.u.distance.range);
        }
        break;
    case MR60_TYPE_PRESENCE:
        printf("presence: %u\n", (unsigned)r.u.presence);
        break;
#endif
    default:
        break;
    }
    fflush(stdout);
}

static void radar_task(void *arg)
{
    (void)arg;
    uint8_t        buf[RADAR_READ_CHUNK];
    mmwave_frame_t frame;

    for (;;) {
        int n = uart_read_bytes(RADAR_UART_NUM, buf, sizeof buf,
                                pdMS_TO_TICKS(RADAR_READ_TIMEOUT_MS));
        if (n <= 0) {
            /* Idle gap: whatever is buffered cannot be part of a frame. */
            mmwave_parser_reset(&s_parser);
            continue;
        }
        for (int i = 0; i < n; i++) {
            if (mmwave_parser_feed(&s_parser, buf[i], &frame)) {
                handle_frame(&frame);
            }
        }
    }
}

esp_err_t radar_task_start(void)
{
    const uart_config_t cfg = {
        .baud_rate  = RADAR_UART_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t err;

    /* ESP-IDF 5.2.7 + ESP32-C6 workaround. The UART driver's very first
     * register sync (uart_ll_update, inside uart_hal_init) runs BEFORE the
     * driver selects the UART's source clock, and on the C6 nothing else
     * enables UART1's clock (esp_perip_clk_init is a stub, the ROM only sets
     * up UART0). With no core clock the sync bit never clears, the spin sits
     * in a critical section, and the interrupt watchdog reboots the chip
     * about 5 s after boot. Seen on hardware 2026-09-17, both with
     * uart_driver_install() first and with uart_param_config() first.
     * Later IDF releases reorder this; until then, pick and enable the
     * clock by hand. */
    uart_ll_enable_bus_clock(RADAR_UART_NUM, true);
    uart_ll_set_sclk(UART_LL_GET_HW(RADAR_UART_NUM), (soc_module_clk_t)UART_SCLK_DEFAULT);
    uart_ll_sclk_enable(UART_LL_GET_HW(RADAR_UART_NUM));

    err = uart_param_config(RADAR_UART_NUM, &cfg);
    if (err != ESP_OK) return err;
    err = uart_set_pin(RADAR_UART_NUM, RADAR_UART_TX_GPIO, RADAR_UART_RX_GPIO,
                       UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) return err;
    err = uart_driver_install(RADAR_UART_NUM, RADAR_UART_RX_BUF, 0, 0, NULL, 0);
    if (err != ESP_OK) return err;

    mmwave_parser_init(&s_parser);

    BaseType_t ok = xTaskCreate(radar_task, "radar", RADAR_TASK_STACK, NULL,
                                RADAR_TASK_PRIO, NULL);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void radar_task_get_stats(mmwave_stats_t *out)
{
    *out = s_parser.stats;      /* diagnostics only; a torn read is harmless */
}
