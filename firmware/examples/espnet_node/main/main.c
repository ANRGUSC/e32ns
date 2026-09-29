/*
 * espnet_node — the PROVEN emulation node app (spike #2, RESEARCH_LOG V-004).
 *
 * Brings up the emulated OpenCores Ethernet (open_eth) + DHCP, waits for an IP, then
 * starts the espnet_shim and runs ESP-NOW. Under QEMU the shim (linked via --wrap)
 * intercepts esp_now_send and pipes frames to the host medium at 10.0.2.2:52333.
 *
 * Verified: this exact firmware, booted under Espressif QEMU 9.0.0, delivered a HELLO
 * and an ESP-NOW broadcast TX to the host-side esp_netsim codec (docs/spike2_evidence.log).
 *
 * Build (Linux + ESP-IDF v5.4.2):
 *   idf.py set-target esp32s3 && idf.py build
 * Run (see firmware/qemu/run_qemu.sh, add: -nic user,model=open_eth), with the host
 * medium/listener already bound to 127.0.0.1:52333.
 */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_eth.h"
#include "esp_now.h"
#include "espnet_shim.h"

static const char *T = "espnet_node";
static EventGroupHandle_t s_eg;
#define GOT_IP_BIT BIT0

/* Node identity + medium endpoint would come from sdkconfig/NVS in a real deployment. */
#ifndef ESPNET_NODE_ID
#define ESPNET_NODE_ID 1
#endif

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data) {
    ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
    ESP_LOGI(T, "GOT_IP " IPSTR, IP2STR(&e->ip_info.ip));
    xEventGroupSetBits(s_eg, GOT_IP_BIT);
}

static void on_espnow_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    ESP_LOGI(T, "esp_now recv %d bytes, first=%d", len, len > 0 ? data[0] : -1);
}

/* Bring up open_eth + DHCP; block until we have an IP (brief §1). */
static bool ethernet_up(void) {
    s_eg = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_config_t netif_cfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&netif_cfg);

    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.autonego_timeout_ms = 100;                 // IDF openeth idiom
    esp_eth_mac_t *mac = esp_eth_mac_new_openeth(&mac_cfg);
    esp_eth_phy_t *phy = esp_eth_phy_new_dp83848(&phy_cfg);
    esp_eth_config_t cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = NULL;
    ESP_ERROR_CHECK(esp_eth_driver_install(&cfg, &eth));
    ESP_ERROR_CHECK(esp_netif_attach(netif, esp_eth_new_netif_glue(eth)));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP,
                                               on_got_ip, NULL));
    ESP_ERROR_CHECK(esp_eth_start(eth));

    ESP_LOGI(T, "waiting for DHCP...");
    EventBits_t bits = xEventGroupWaitBits(s_eg, GOT_IP_BIT, pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(8000));
    return (bits & GOT_IP_BIT) != 0;
}

void app_main(void) {
    if (!ethernet_up()) {
        ESP_LOGE(T, "no IP from DHCP; medium unreachable");
        return;
    }
    espnet_shim_config_t sc = {
        .node_id = ESPNET_NODE_ID,
        .mac = {0xaa, 0xbb, 0xcc, 0x00, 0x00, (uint8_t)ESPNET_NODE_ID},
        .channel = 1,
        .medium_host = "10.0.2.2",   // QEMU user-net host alias
        .medium_port = 52333,
    };
    espnet_shim_start(&sc);

    ESP_ERROR_CHECK(esp_now_init());                 // -> __wrap_esp_now_init (shim)
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_espnow_recv));

    /* Periodic ESP-NOW broadcast — each send is intercepted and sent to the medium. */
    uint8_t counter = 0;
    for (;;) {
        esp_now_send((const uint8_t *)"\xff\xff\xff\xff\xff\xff", &counter, 1);
        counter++;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
