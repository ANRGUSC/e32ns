/* Firmware D: time esp_now_send to its send callback, for broadcast and for a unicast nobody ACKs. */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "espnow_common.h"

#define ROUNDS 100

static const char *TAG = "txprobe";
static const uint8_t BCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const uint8_t ABSENT[6] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x99};  /* no node has this MAC */
static const int LENS[] = {16, 128, 250};

static SemaphoreHandle_t s_done;
static int64_t s_t0, s_t1;
static esp_now_send_status_t s_status;

static void on_send(const uint8_t *mac, esp_now_send_status_t status) {
    s_t1 = esp_timer_get_time();
    s_status = status;
    xSemaphoreGive(s_done);
}

static void add_peer(const uint8_t *mac) {
    esp_now_peer_info_t peer = { .channel = ESPNOW_CHANNEL };
    memcpy(peer.peer_addr, mac, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
}

static void probe(const uint8_t *dst, int len) {
    static uint8_t buf[250];
    s_t0 = esp_timer_get_time();
    ESP_ERROR_CHECK(esp_now_send(dst, buf, len));
    /* One send in flight, so the callback's time belongs to this send. */
    xSemaphoreTake(s_done, portMAX_DELAY);
    ESP_LOGI(TAG, "TXCB dst=%c len=%d status=%s us=%lld", dst == BCAST ? 'B' : 'U', len,
             s_status == ESP_NOW_SEND_SUCCESS ? "OK" : "FAIL", s_t1 - s_t0);
}

void app_main(void) {
    s_done = xSemaphoreCreateBinary();
    espnow_bringup();
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_send));
    add_peer(BCAST);
    add_peer(ABSENT);

    for (int r = 0; r < ROUNDS; r++) {
        for (int i = 0; i < 3; i++) {
            probe(BCAST, LENS[i]);
            probe(ABSENT, LENS[i]);
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }
    ESP_LOGI(TAG, "DONE");
}
