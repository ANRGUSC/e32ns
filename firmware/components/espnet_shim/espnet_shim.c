/*
 * espnet_shim.c — esp_now_* overrides that bridge to the host medium (D-017, D-021).
 *
 * Wire body is compact BIG-ENDIAN BINARY (D-019) — matches src/esp_netsim/protocol.py.
 * No JSON, no hex on the hot path (that was a smell on the emulated Xtensa core). The
 * transport layer adds the 4-byte length prefix (docs/PROTOCOL.md).
 *
 * Interposition is via --wrap (D-021): these functions are __wrap_esp_now_* and the
 * component's CMakeLists passes -Wl,--wrap=esp_now_*, so the app's calls land here.
 *
 * The TX timestamp is esp_timer_get_time() (D-022): under the QMP barrier the guest clock
 * freezes while paused, so guest time == virtual time by construction.
 *
 * SOURCE-ONLY in CI; built under Linux ESP-IDF + QEMU. Proven end-to-end: HELLO + TX
 * decoded by the host codec (docs/spike2_evidence.log).
 */
#include "espnet_shim.h"
#include "esp_now.h"
#include "espnet_transport.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "espnet_shim";

/* Wire tags — keep in sync with protocol._TAG. */
enum { T_HELLO = 1, T_WELCOME = 2, T_ADD_PEER = 3, T_DEL_PEER = 4, T_TX = 5,
       T_TX_STATUS = 6, T_RX = 7, T_RUN_SLICE = 8, T_SLICE_DONE = 9, T_SHUTDOWN = 10 };

static esp_now_send_cb_t s_send_cb = NULL;
static esp_now_recv_cb_t s_recv_cb = NULL;
static espnet_shim_config_t s_cfg;
static uint32_t s_seq = 0;

/* ---- big-endian writers (return advanced cursor) -------------------------- */
static uint8_t *w8(uint8_t *p, uint8_t v) { *p++ = v; return p; }
static uint8_t *w16(uint8_t *p, uint16_t v) { *p++ = v >> 8; *p++ = v & 0xFF; return p; }
static uint8_t *w32(uint8_t *p, uint32_t v) {
    *p++ = v >> 24; *p++ = v >> 16; *p++ = v >> 8; *p++ = v; return p;
}
static uint8_t *w64(uint8_t *p, uint64_t v) {
    for (int i = 7; i >= 0; i--) {
        *p++ = (v >> (i * 8)) & 0xFF;
    }
    return p;
}
static uint8_t *wf32(uint8_t *p, float f) {
    uint32_t u; memcpy(&u, &f, 4); return w32(p, u);
}
static uint8_t *wmac(uint8_t *p, const uint8_t *m) { memcpy(p, m, 6); return p + 6; }

/* ---- big-endian readers --------------------------------------------------- */
static uint16_t r16(const uint8_t *p) { return ((uint16_t)p[0] << 8) | p[1]; }
static uint32_t r32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

/* ---- ESP-NOW API surface (--wrap targets) --------------------------------- */
esp_err_t __wrap_esp_now_init(void) {
    ESP_LOGI(TAG, "esp_now_init (shim): node %d -> %s:%u",
             s_cfg.node_id, s_cfg.medium_host, s_cfg.medium_port);
    uint8_t buf[16], *p = buf;
    p = w8(p, T_HELLO);
    p = w16(p, (uint16_t)s_cfg.node_id);
    p = wmac(p, s_cfg.mac);
    p = w8(p, 0x01);  /* caps: espnow=1, wifi=0 (kept honest until M4) */
    espnet_transport_send((const char *)buf, (int)(p - buf));
    return ESP_OK;
}

esp_err_t __wrap_esp_now_deinit(void) { espnet_transport_close(); return ESP_OK; }

esp_err_t __wrap_esp_now_register_send_cb(esp_now_send_cb_t cb) { s_send_cb = cb; return ESP_OK; }
esp_err_t __wrap_esp_now_register_recv_cb(esp_now_recv_cb_t cb) { s_recv_cb = cb; return ESP_OK; }
esp_err_t __wrap_esp_now_unregister_send_cb(void) { s_send_cb = NULL; return ESP_OK; }
esp_err_t __wrap_esp_now_unregister_recv_cb(void) { s_recv_cb = NULL; return ESP_OK; }

esp_err_t __wrap_esp_now_add_peer(const esp_now_peer_info_t *peer) {
    uint8_t buf[32], *p = buf;
    p = w8(p, T_ADD_PEER);
    p = w16(p, (uint16_t)s_cfg.node_id);
    p = wmac(p, peer->peer_addr);
    p = w8(p, peer->channel);
    p = w8(p, peer->encrypt ? 1 : 0);
    if (peer->encrypt) { memcpy(p, peer->lmk, 16); } else { memset(p, 0, 16); }
    p += 16;
    espnet_transport_send((const char *)buf, (int)(p - buf));
    return ESP_OK;
}

esp_err_t __wrap_esp_now_del_peer(const uint8_t *peer_addr) {
    uint8_t buf[12], *p = buf;
    p = w8(p, T_DEL_PEER);
    p = w16(p, (uint16_t)s_cfg.node_id);
    p = wmac(p, peer_addr);
    espnet_transport_send((const char *)buf, (int)(p - buf));
    return ESP_OK;
}

esp_err_t __wrap_esp_now_set_pmk(const uint8_t *pmk) { (void)pmk; return ESP_OK; }

/* The heart: serialize a TX frame to the medium (brief §3 semantics live host-side). */
esp_err_t __wrap_esp_now_send(const uint8_t *peer_addr, const uint8_t *data, size_t len) {
    if (len > 250) return ESP_ERR_INVALID_ARG;   /* ESP_NOW_MAX_DATA_LEN (v1) */
    static const uint8_t BCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
    uint8_t buf[300], *p = buf;
    p = w8(p, T_TX);
    p = w16(p, (uint16_t)s_cfg.node_id);
    p = w64(p, (uint64_t)esp_timer_get_time());   /* virtual time (D-022) */
    p = wmac(p, s_cfg.mac);
    p = wmac(p, peer_addr ? peer_addr : BCAST);    /* NULL peer => broadcast */
    p = w8(p, s_cfg.channel);
    p = wf32(p, 1.0f);                             /* default rate 1 Mbps (brief §3) */
    p = w32(p, ++s_seq);
    p = w16(p, (uint16_t)len);
    memcpy(p, data, len); p += len;
    espnet_transport_send((const char *)buf, (int)(p - buf));
    return ESP_OK;   /* the async TX_STATUS drives the real send-cb verdict */
}

/* ---- background reader: TX_STATUS -> send_cb, RX -> recv_cb --------------- */
static void handle_message(const uint8_t *b, int len) {
    if (len < 1) return;
    uint8_t tag = b[0];
    if (tag == T_TX_STATUS && s_send_cb) {
        /* node(u16) seq(u32) status(u8) t(u64) */
        uint8_t st = (len >= 8) ? b[7] : 0;
        uint8_t peer[6] = {0};  /* a fuller impl maps seq->peer_addr */
        s_send_cb(peer, st ? ESP_NOW_SEND_SUCCESS : ESP_NOW_SEND_FAIL);  /* brief §3 */
    } else if (tag == T_RX && s_recv_cb && len >= 34) {
        /* node(u16) t(u64) src(6) dst(6) ch(u8) rate(f32) rssi(f32) dlen(u16) data */
        const uint8_t *src = b + 11;
        uint16_t dlen = r16(b + 32);
        const uint8_t *data = b + 34;
        if (34 + dlen <= len) {
            /* IDF >=5.x passes esp_now_recv_info_t*; adapt per IDF version if needed. */
            s_recv_cb((const esp_now_recv_info_t *)src, data, (int)dlen);
        }
    } else if (tag == T_SHUTDOWN) {
        espnet_transport_close();
    }
}

static void reader_task(void *arg) {
    (void)arg;
    uint8_t line[1024];
    for (;;) {
        int n = espnet_transport_recv_message((char *)line, sizeof line);
        if (n > 0) handle_message(line, n);
        else vTaskDelay(pdMS_TO_TICKS(1));
    }
}

int espnet_shim_start(const espnet_shim_config_t *cfg) {
    s_cfg = *cfg;
    if (espnet_transport_open(cfg->medium_host, cfg->medium_port) != 0) {
        ESP_LOGE(TAG, "transport open failed");
        return -1;
    }
    xTaskCreate(reader_task, "espnet_rx", 4096, NULL, 5, NULL);
    return 0;
}
