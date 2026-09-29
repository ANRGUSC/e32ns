#pragma once
#include <stdint.h>

#define ESPNOW_CHANNEL 1

typedef struct __attribute__((packed)) {
    char kind;        /* 'P' ping, 'R' reply */
    uint32_t seq;
    int64_t t0_us;    /* pinger's esp_timer at send */
} ping_msg_t;

/* Standard hardware ESP-NOW bring-up (NVS, netif, Wi-Fi STA, ESP-NOW). */
void espnow_bringup(void);
