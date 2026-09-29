/*
 * espnet_transport_socket.c — TCP-socket transport backend (D-003, brief §9 risk #2).
 *
 * Uses the standard lwIP BSD-socket API, which ESP-IDF provides. Inside QEMU the
 * socket rides the emulated open_eth NIC through user-mode NAT to the host medium
 * (host alias 10.0.2.2). Length-prefixed framing per docs/PROTOCOL.md.
 *
 * SOURCE-ONLY (built under ESP-IDF; not in Python CI). A UART backend implementing
 * the same three functions can replace this file after the transport spike.
 */
#include "espnet_transport.h"

#include <string.h>
#include <stdint.h>

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "esp_log.h"

static const char *TAG = "espnet_tx";
static int s_fd = -1;

/* Persistent RX assembly buffer for length-prefixed framing. */
static uint8_t s_buf[2048];
static int s_have = 0;

int espnet_transport_open(const char *host, unsigned short port) {
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = inet_addr(host);

    s_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s_fd < 0) { ESP_LOGE(TAG, "socket() failed"); return -1; }
    if (connect(s_fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        ESP_LOGE(TAG, "connect(%s:%u) failed", host, port);
        close(s_fd); s_fd = -1; return -1;
    }
    ESP_LOGI(TAG, "connected to medium %s:%u", host, port);
    return 0;
}

int espnet_transport_send(const char *json, int len) {
    if (s_fd < 0) return -1;
    uint8_t prefix[4] = {
        (uint8_t)((len >> 24) & 0xFF), (uint8_t)((len >> 16) & 0xFF),
        (uint8_t)((len >> 8) & 0xFF),  (uint8_t)(len & 0xFF)
    };
    if (send(s_fd, prefix, 4, 0) != 4) return -1;
    return send(s_fd, json, len, 0);
}

int espnet_transport_recv_message(char *out, int max) {
    if (s_fd < 0) return -1;

    /* Try to satisfy a full frame from the buffer; otherwise read more. */
    for (;;) {
        if (s_have >= 4) {
            uint32_t plen = ((uint32_t)s_buf[0] << 24) | ((uint32_t)s_buf[1] << 16)
                          | ((uint32_t)s_buf[2] << 8) | (uint32_t)s_buf[3];
            if (plen + 4 <= (uint32_t)s_have) {
                int n = (int)plen < (max - 1) ? (int)plen : (max - 1);
                memcpy(out, s_buf + 4, n);
                out[n] = '\0';
                /* shift remainder down */
                int consumed = 4 + (int)plen;
                memmove(s_buf, s_buf + consumed, s_have - consumed);
                s_have -= consumed;
                return n;
            }
        }
        int r = recv(s_fd, s_buf + s_have, sizeof(s_buf) - s_have, 0);
        if (r <= 0) return (r == 0) ? 0 : -1;
        s_have += r;
    }
}

void espnet_transport_close(void) {
    if (s_fd >= 0) { close(s_fd); s_fd = -1; }
    s_have = 0;
}
