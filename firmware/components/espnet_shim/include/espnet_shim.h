/*
 * espnet_shim — ESP-IDF component that redirects ESP-NOW traffic to the host-side
 * esp-netsim medium instead of a real radio (research-log D-002, D-017).
 *
 * The component PROVIDES the public esp_now_* symbols. An unmodified ESP-NOW
 * application links against these; esp_now_send serializes the frame to a host
 * transport (the same JSON wire protocol as docs/PROTOCOL.md) and a background
 * task translates TX_STATUS/RX messages back into the app's send/recv callbacks.
 *
 * This file is delivered as source; it is built inside a Linux ESP-IDF + QEMU
 * environment (see docs/QEMU_INTEGRATION.md). It is NOT compiled in the Python
 * repo's CI. Keep it C99, no third-party JSON dependency.
 */
#ifndef ESPNET_SHIM_H
#define ESPNET_SHIM_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Node identity + transport configuration, provided by the app or sdkconfig. */
typedef struct {
    int      node_id;         /* logical node id known to the medium */
    uint8_t  mac[6];          /* this node's MAC (matches the scenario config) */
    uint8_t  channel;         /* current Wi-Fi channel */
    /* Transport: how bytes leave QEMU to reach the host medium (brief §9 risk #2).
     * v1 backend is a TCP socket carried over the emulated open_eth NIC, or a
     * dedicated UART. Selected at init; see espnet_transport.h. */
    const char *medium_host;  /* e.g. "10.0.2.2" (QEMU user-net host alias) */
    uint16_t    medium_port;  /* e.g. 52333 */
} espnet_shim_config_t;

/* Initialize the shim transport and background reader. Call once at boot,
 * BEFORE esp_now_init (or let esp_now_init call it). Returns 0 on success. */
int espnet_shim_start(const espnet_shim_config_t *cfg);

#ifdef __cplusplus
}
#endif
#endif /* ESPNET_SHIM_H */
