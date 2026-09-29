/* Firmware A: broadcast a timestamped ping every PERIOD_MS and log the RTT of each reply. */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "espnow_common.h"

#define PERIOD_MS 100

static const char *TAG = "ping";
static const uint8_t BCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    ping_msg_t m;
    if (len != sizeof m) return;
    memcpy(&m, data, sizeof m);
    if (m.kind != 'R') return;
    ESP_LOGI(TAG, "RTT seq=%lu from " MACSTR " rssi=%d rtt_us=%lld", (unsigned long)m.seq,
             MAC2STR(info->src_addr), info->rx_ctrl->rssi, esp_timer_get_time() - m.t0_us);
}

void app_main(void) {
    espnow_bringup();
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_recv));
    esp_now_peer_info_t peer = { .channel = ESPNOW_CHANNEL };
    memcpy(peer.peer_addr, BCAST, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    for (uint32_t seq = 1;; seq++) {
        ESP_LOGI(TAG, "PING seq=%lu", (unsigned long)seq);
        ping_msg_t m = { .kind = 'P', .seq = seq, .t0_us = esp_timer_get_time() };
        esp_now_send(BCAST, (const uint8_t *)&m, sizeof m);
        vTaskDelay(pdMS_TO_TICKS(PERIOD_MS));
    }
}
