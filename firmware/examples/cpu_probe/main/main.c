/* Firmware E: count fixed work loops completed in each 100 ms of guest esp_timer time. */
#include <inttypes.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

void app_main(void) {
    vTaskDelay(pdMS_TO_TICKS(300));
    for (int r = 0; r < 60; r++) {
        volatile uint32_t x = 0;
        uint32_t n = 0;
        int64_t t0 = esp_timer_get_time();
        while (esp_timer_get_time() - t0 < 100000) {
            for (int i = 0; i < 100; i++) x += i;
            n++;
        }
        ESP_LOGI("cpu", "round %d loops %" PRIu32, r, n);
        vTaskDelay(1);   /* let the idle task feed the watchdog */
    }
}
