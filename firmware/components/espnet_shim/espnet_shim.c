/*
 * espnet_shim: redirects esp_now_* and the Wi-Fi bring-up calls an ESP-NOW app makes to the
 * host medium, so an unmodified hardware app runs under QEMU (D-021, D-028).
 *
 * Linked via -Wl,--wrap (component-provided symbols lose to IDF's archive, D-021). Node
 * identity comes from the NVS namespace "espnet", written per node by the host launcher.
 * Wire layout mirrors src/esp_netsim/protocol.py: big-endian, 4-byte length prefix.
 */
#include <string.h>
#include <stddef.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_eth.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "lwip/sockets.h"

static const char *TAG = "espnet_shim";

enum { T_HELLO = 1, T_ADD_PEER = 3, T_DEL_PEER = 4, T_TX = 5, T_TX_STATUS = 6, T_RX = 7,
       T_SHUTDOWN = 10 };

typedef struct __attribute__((packed)) {
    uint8_t tag; uint16_t node; uint8_t mac[6]; uint8_t caps;
} hello_msg_t;

typedef struct __attribute__((packed)) {
    uint8_t tag; uint16_t node; uint8_t peer[6]; uint8_t channel; uint8_t encrypt;
    uint8_t lmk[16];
} add_peer_msg_t;

typedef struct __attribute__((packed)) {
    uint8_t tag; uint16_t node; uint8_t peer[6];
} del_peer_msg_t;

typedef struct __attribute__((packed)) {
    uint8_t tag; uint16_t node; uint64_t t; uint8_t src[6]; uint8_t dst[6]; uint8_t channel;
    uint32_t rate; uint32_t seq; uint16_t len; uint8_t data[ESP_NOW_MAX_DATA_LEN];
} tx_msg_t;

typedef struct __attribute__((packed)) {
    uint8_t tag; uint16_t node; uint32_t seq; uint8_t status; uint64_t t;
} tx_status_msg_t;

typedef struct __attribute__((packed)) {
    uint8_t tag; uint16_t node; uint64_t t; uint8_t src[6]; uint8_t dst[6]; uint8_t channel;
    uint32_t rate; uint32_t rssi; uint16_t len; uint8_t data[];
} rx_msg_t;

#define SEQ_RING 16  /* in-flight sends whose TX_STATUS is still pending */

static struct {
    uint16_t node_id;
    uint8_t mac[6];
    uint8_t channel;
    uint16_t port;
} s_id;

static int s_fd = -1;
static uint32_t s_seq;
static uint8_t s_seq_dst[SEQ_RING][6];
static esp_now_send_cb_t s_send_cb;
static esp_now_recv_cb_t s_recv_cb;
static EventGroupHandle_t s_eg;
static esp_netif_t *s_netif;
static esp_eth_handle_t s_eth;
static bool s_eth_started;
static StaticEventGroup_t s_eg_mem;
static StackType_t s_rx_stack[4096];
static StaticTask_t s_rx_task;

static uint32_t f32_be(float f) { uint32_t u; memcpy(&u, &f, 4); return __builtin_bswap32(u); }
static float be_f32(uint32_t be) { uint32_t u = __builtin_bswap32(be); float f; memcpy(&f, &u, 4); return f; }

static void send_msg(const void *body, size_t len) {
    uint8_t buf[4 + sizeof(tx_msg_t)];
    uint32_t prefix = __builtin_bswap32((uint32_t)len);
    memcpy(buf, &prefix, 4);
    memcpy(buf + 4, body, len);
    send(s_fd, buf, 4 + len, 0);
}

static void load_identity(void) {
    esp_err_t err = nvs_flash_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(err);
    nvs_handle_t h;
    size_t mac_len = sizeof s_id.mac;
    ESP_ERROR_CHECK(nvs_open("espnet", NVS_READONLY, &h));
    ESP_ERROR_CHECK(nvs_get_u16(h, "node_id", &s_id.node_id));
    ESP_ERROR_CHECK(nvs_get_blob(h, "mac", s_id.mac, &mac_len));
    ESP_ERROR_CHECK(nvs_get_u8(h, "channel", &s_id.channel));
    ESP_ERROR_CHECK(nvs_get_u16(h, "port", &s_id.port));
    nvs_close(h);
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data) {
    xEventGroupSetBits(s_eg, BIT0);
    /* Station apps wait for Wi-Fi events; the open_eth NIC stands in for the station. */
    wifi_event_sta_connected_t connected = { .channel = s_id.channel };
    esp_event_post(WIFI_EVENT, WIFI_EVENT_STA_CONNECTED, &connected, sizeof connected, 0);
    esp_event_post(IP_EVENT, IP_EVENT_STA_GOT_IP, data, sizeof(ip_event_got_ip_t), 0);
}

/* ESP-NOW control traffic and Wi-Fi traffic both ride QEMU's open_eth NIC (D-028, D-030). */
static void ethernet_init(void) {
    if (s_netif) return;
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(err);
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(err);

    s_eg = xEventGroupCreateStatic(&s_eg_mem);
    esp_netif_config_t netif_cfg = ESP_NETIF_DEFAULT_ETH();
    s_netif = esp_netif_new(&netif_cfg);
    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.autonego_timeout_ms = 100;
    esp_eth_config_t cfg = ETH_DEFAULT_CONFIG(esp_eth_mac_new_openeth(&mac_cfg),
                                              esp_eth_phy_new_dp83848(&phy_cfg));
    ESP_ERROR_CHECK(esp_eth_driver_install(&cfg, &s_eth));
    ESP_ERROR_CHECK(esp_netif_attach(s_netif, esp_eth_new_netif_glue(s_eth)));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_got_ip, NULL));
}

static void ethernet_start(void) {
    ethernet_init();
    if (!s_eth_started) {
        ESP_ERROR_CHECK(esp_eth_start(s_eth));
        s_eth_started = true;
    }
}

static void connect_medium(void) {
    struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(s_id.port),
                                .sin_addr.s_addr = inet_addr("10.0.2.2") };  /* slirp host */
    int one = 1;
    s_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    setsockopt(s_fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    if (connect(s_fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        ESP_LOGE(TAG, "cannot reach medium on port %u", s_id.port);
        abort();
    }
}

static int recv_exact(uint8_t *buf, size_t len) {
    for (size_t got = 0; got < len;) {
        int r = recv(s_fd, buf + got, len - got, 0);
        if (r <= 0) return -1;
        got += r;
    }
    return 0;
}

static void deliver_rx(const rx_msg_t *m) {
    wifi_pkt_rx_ctrl_t ctrl = { .rssi = (int)be_f32(m->rssi), .channel = m->channel };
    esp_now_recv_info_t info = { .src_addr = (uint8_t *)m->src, .des_addr = (uint8_t *)m->dst,
                                 .rx_ctrl = &ctrl };
    s_recv_cb(&info, m->data, __builtin_bswap16(m->len));
}

static void reader_task(void *arg) {
    static uint8_t body[sizeof(rx_msg_t) + ESP_NOW_MAX_DATA_LEN];
    uint32_t len;
    while (recv_exact((uint8_t *)&len, 4) == 0) {
        len = __builtin_bswap32(len);
        if (len > sizeof body || recv_exact(body, len) != 0) break;
        if (body[0] == T_TX_STATUS && s_send_cb) {
            const tx_status_msg_t *st = (const tx_status_msg_t *)body;
            uint32_t seq = __builtin_bswap32(st->seq);
            s_send_cb(s_seq_dst[seq % SEQ_RING],
                      st->status ? ESP_NOW_SEND_SUCCESS : ESP_NOW_SEND_FAIL);
        } else if (body[0] == T_RX && s_recv_cb) {
            deliver_rx((const rx_msg_t *)body);
        } else if (body[0] == T_SHUTDOWN) {
            break;
        }
    }
    ESP_LOGI(TAG, "medium closed the link");
    vTaskDelete(NULL);
}

esp_err_t __wrap_esp_now_init(void) {
    load_identity();
    ethernet_start();
    xEventGroupWaitBits(s_eg, BIT0, pdFALSE, pdTRUE, portMAX_DELAY);
    connect_medium();
    ESP_LOGI(TAG, "node %u " MACSTR " ch %u on medium port %u",
             s_id.node_id, MAC2STR(s_id.mac), s_id.channel, s_id.port);
    hello_msg_t m = { .tag = T_HELLO, .node = __builtin_bswap16(s_id.node_id), .caps = 0x01 };
    memcpy(m.mac, s_id.mac, 6);
    send_msg(&m, sizeof m);
    xTaskCreateStatic(reader_task, "espnet_rx", sizeof s_rx_stack, NULL, 5, s_rx_stack, &s_rx_task);
    return ESP_OK;
}

esp_err_t __wrap_esp_now_deinit(void) { shutdown(s_fd, SHUT_RDWR); return ESP_OK; }
esp_err_t __wrap_esp_now_register_send_cb(esp_now_send_cb_t cb) { s_send_cb = cb; return ESP_OK; }
esp_err_t __wrap_esp_now_register_recv_cb(esp_now_recv_cb_t cb) { s_recv_cb = cb; return ESP_OK; }
esp_err_t __wrap_esp_now_unregister_send_cb(void) { s_send_cb = NULL; return ESP_OK; }
esp_err_t __wrap_esp_now_unregister_recv_cb(void) { s_recv_cb = NULL; return ESP_OK; }
esp_err_t __wrap_esp_now_set_pmk(const uint8_t *pmk) { return ESP_OK; }

esp_err_t __wrap_esp_now_add_peer(const esp_now_peer_info_t *peer) {
    add_peer_msg_t m = { .tag = T_ADD_PEER, .node = __builtin_bswap16(s_id.node_id),
                         .channel = peer->channel, .encrypt = peer->encrypt };
    memcpy(m.peer, peer->peer_addr, 6);
    if (peer->encrypt) memcpy(m.lmk, peer->lmk, 16);
    send_msg(&m, sizeof m);
    return ESP_OK;
}

esp_err_t __wrap_esp_now_del_peer(const uint8_t *peer_addr) {
    del_peer_msg_t m = { .tag = T_DEL_PEER, .node = __builtin_bswap16(s_id.node_id) };
    memcpy(m.peer, peer_addr, 6);
    send_msg(&m, sizeof m);
    return ESP_OK;
}

esp_err_t __wrap_esp_now_send(const uint8_t *peer_addr, const uint8_t *data, size_t len) {
    static const uint8_t BCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
    if (len > ESP_NOW_MAX_DATA_LEN) return ESP_ERR_ESPNOW_ARG;
    tx_msg_t m = { .tag = T_TX, .node = __builtin_bswap16(s_id.node_id),
                   .t = __builtin_bswap64((uint64_t)esp_timer_get_time()),  /* D-022 */
                   .channel = s_id.channel, .rate = f32_be(1.0f),
                   .seq = __builtin_bswap32(++s_seq), .len = __builtin_bswap16((uint16_t)len) };
    memcpy(m.src, s_id.mac, 6);
    memcpy(m.dst, peer_addr ? peer_addr : BCAST, 6);
    memcpy(s_seq_dst[s_seq % SEQ_RING], m.dst, 6);
    memcpy(m.data, data, len);
    send_msg(&m, offsetof(tx_msg_t, data) + len);
    return ESP_OK;
}

/* No radio under QEMU: the station is the open_eth NIC, whose frames the host medium carries
 * over the modeled AP link. The scenario's channel is authoritative. */
esp_err_t __wrap_esp_wifi_init(const wifi_init_config_t *config) { return ESP_OK; }
esp_err_t __wrap_esp_wifi_set_storage(wifi_storage_t storage) { return ESP_OK; }
esp_err_t __wrap_esp_wifi_set_mode(wifi_mode_t mode) { return ESP_OK; }
esp_err_t __wrap_esp_wifi_set_channel(uint8_t primary, wifi_second_chan_t second) { return ESP_OK; }
esp_err_t __wrap_esp_wifi_set_config(wifi_interface_t iface, wifi_config_t *conf) { return ESP_OK; }

esp_err_t __wrap_esp_wifi_start(void) {
    return esp_event_post(WIFI_EVENT, WIFI_EVENT_STA_START, NULL, 0, portMAX_DELAY);
}

esp_err_t __wrap_esp_wifi_connect(void) {
    ethernet_start();
    return ESP_OK;
}

esp_netif_t *__wrap_esp_netif_create_default_wifi_sta(void) {
    ethernet_init();
    return s_netif;
}
