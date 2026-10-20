/* Firmware B: answer every ping with a unicast reply of the same length to its sender. */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_now.h"
#include "espnow_common.h"

typedef struct {
    uint8_t src[6];
    int len;
    uint8_t data[ESP_NOW_MAX_DATA_LEN];
} reply_job_t;

static const char *TAG = "pong";
static QueueHandle_t s_jobs;

/* The receive callback runs in the Wi-Fi task on hardware; sending from it is discouraged. */
static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    reply_job_t job;
    if (len < (int)sizeof(ping_msg_t) || data[0] != 'P') return;
    job.len = len;
    memcpy(job.data, data, len);
    memcpy(job.src, info->src_addr, 6);
    xQueueSend(s_jobs, &job, 0);
}

static void on_send(const uint8_t *mac, esp_now_send_status_t status) {
    ESP_LOGI(TAG, "reply to " MACSTR " %s", MAC2STR(mac),
             status == ESP_NOW_SEND_SUCCESS ? "ACKED" : "FAILED");
}

void app_main(void) {
    s_jobs = xQueueCreate(8, sizeof(reply_job_t));
    espnow_bringup();
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_recv));
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_send));

    reply_job_t job;
    for (;;) {
        xQueueReceive(s_jobs, &job, portMAX_DELAY);
        esp_now_peer_info_t peer = { .channel = ESPNOW_CHANNEL };
        memcpy(peer.peer_addr, job.src, 6);
        esp_now_add_peer(&peer);  /* ESP_ERR_ESPNOW_EXIST on repeat is expected */
        job.data[0] = 'R';
        esp_now_send(job.src, job.data, job.len);
    }
}
