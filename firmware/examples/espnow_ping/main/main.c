/*
 * espnow_ping — a minimal ESP-NOW app that runs UNMODIFIED on real hardware and
 * under esp-netsim's QEMU path (D-017). On hardware it uses the real radio; under
 * emulation the espnet_shim component supplies esp_now_* and bridges to the host
 * medium. The only emulation-specific line is espnet_shim_start() guarded by a
 * build flag, so the same source targets both.
 *
 * Behavior: node broadcasts a counter every 100 ms and logs anything it receives.
 */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_now.h"

#ifdef CONFIG_ESPNET_EMULATION
#include "espnet_shim.h"
#endif

static const char *TAG = "espnow_ping";
static const uint8_t BCAST[6] = {0xff,0xff,0xff,0xff,0xff,0xff};

static void on_send(const uint8_t *mac, esp_now_send_status_t status) {
    ESP_LOGI(TAG, "send-cb: %s", status == ESP_NOW_SEND_SUCCESS ? "SUCCESS" : "FAIL");
}

static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    ESP_LOGI(TAG, "recv %d bytes, first=%d", len, len > 0 ? data[0] : -1);
}

void app_main(void) {
#ifdef CONFIG_ESPNET_EMULATION
    /* Identity + medium endpoint would normally come from sdkconfig / NVS. */
    espnet_shim_config_t cfg = {
        .node_id = CONFIG_ESPNET_NODE_ID,
        .mac = { 0xaa,0xbb,0xcc,0x00,0x00, (uint8_t)CONFIG_ESPNET_NODE_ID },
        .channel = 1,
        .medium_host = "10.0.2.2",   /* QEMU user-net host alias */
        .medium_port = 52333,
    };
    espnet_shim_start(&cfg);
#endif

    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_send));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_recv));

    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, BCAST, 6);
    peer.channel = 1;
    peer.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    uint8_t counter = 0;
    for (;;) {
        esp_now_send(BCAST, &counter, 1);
        counter++;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
