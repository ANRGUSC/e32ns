/* Firmware C: join the AP as a station, then fetch a page from the real Internet every 2 s. */
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#define URL "http://example.com/"

static const char *TAG = "wifi_http";
static EventGroupHandle_t s_eg;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_eg, BIT0);
    }
}

static void wifi_join(void) {
    s_eg = xEventGroupCreate();
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    wifi_config_t sta = { .sta = { .ssid = "esp-netsim", .password = "esp-netsim" } };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    ESP_ERROR_CHECK(esp_wifi_start());
    while (!(xEventGroupWaitBits(s_eg, BIT0, pdFALSE, pdTRUE, pdMS_TO_TICKS(3000)) & BIT0)) {
        ESP_LOGW(TAG, "no IP yet: the AP is not answering DHCP");
    }
}

void app_main(void) {
    wifi_join();
    esp_http_client_config_t cfg = { .url = URL, .timeout_ms = 5000 };
    for (int i = 1;; i++) {
        esp_http_client_handle_t client = esp_http_client_init(&cfg);
        int64_t t0 = esp_timer_get_time();
        esp_err_t err = esp_http_client_perform(client);
        int64_t ms = (esp_timer_get_time() - t0) / 1000;
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "GET %d %s -> HTTP %d in %lld ms", i, URL,
                     esp_http_client_get_status_code(client), ms);
        } else {
            ESP_LOGW(TAG, "GET %d %s failed after %lld ms: %s", i, URL, ms, esp_err_to_name(err));
        }
        esp_http_client_cleanup(client);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
