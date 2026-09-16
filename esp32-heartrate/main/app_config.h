/*
 * app_config.h - board-level constants for the MR60BHA2 (XIAO ESP32-C6).
 *
 * The radar MCU talks to the C6 over what the Arduino sketch called
 * HardwareSerial(0): UART0's default IOMUX pins on the C6, GPIO16 (TX) and
 * GPIO17 (RX), a.k.a. XIAO D6/D7. We drive those pins from UART1 so the
 * radar link is independent of the IDF console and of ROM download mode.
 * The console itself lives on the native USB-Serial/JTAG (GPIO12/13, fixed).
 */
#ifndef APP_CONFIG_H
#define APP_CONFIG_H

#include "driver/uart.h"

#define RADAR_UART_NUM        UART_NUM_1
#define RADAR_UART_TX_GPIO    16      /* C6 -> radar; unused, radar is never commanded */
#define RADAR_UART_RX_GPIO    17      /* radar -> C6 */
#define RADAR_UART_BAUD       115200
#define RADAR_UART_RX_BUF     4096    /* driver ring buffer; frames are ~13 bytes */

/* How long uart_read_bytes waits for data. A frame is ~1 ms on the wire, so a
 * gap this long means whatever was buffered is not part of a frame. */
#define RADAR_READ_TIMEOUT_MS 100
#define RADAR_READ_CHUNK      256

#define RADAR_TASK_STACK      4096
#define RADAR_TASK_PRIO       5

#endif /* APP_CONFIG_H */
