/*
 * espnet_transport.h — byte transport for the espnet_shim (brief §9 risk #2, D-003).
 *
 * Abstracts HOW frames leave QEMU to reach the host medium so the backend can be
 * swapped after the transport spike: a TCP socket over the emulated open_eth NIC
 * (default), or a dedicated UART/USB-serial-JTAG channel. Framing matches
 * docs/PROTOCOL.md: 4-byte big-endian length prefix + JSON body.
 */
#ifndef ESPNET_TRANSPORT_H
#define ESPNET_TRANSPORT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Open the transport (e.g. connect a TCP socket to host:port via lwIP over
 * open_eth user-net; 10.0.2.2 is the QEMU host alias). Returns 0 on success. */
int espnet_transport_open(const char *host, unsigned short port);

/* Send one protocol message body (JSON, no prefix); the transport adds the
 * 4-byte big-endian length prefix. Returns bytes sent or <0 on error. */
int espnet_transport_send(const char *json, int len);

/* Block until one complete message body is available; copy it (NUL-terminated)
 * into out (max bytes). Returns body length, 0 if none, <0 on error. */
int espnet_transport_recv_message(char *out, int max);

void espnet_transport_close(void);

#ifdef __cplusplus
}
#endif
#endif /* ESPNET_TRANSPORT_H */
