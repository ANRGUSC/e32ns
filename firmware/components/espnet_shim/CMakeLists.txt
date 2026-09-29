idf_component_register(
    SRCS "espnet_shim.c"
    REQUIRES esp_wifi esp_timer esp_eth esp_netif esp_event nvs_flash lwip freertos log)

# --wrap is required: component-provided esp_now_* symbols lose to IDF's archive (D-021).
set(ESPNET_WRAPPED
    esp_now_init esp_now_deinit esp_now_send esp_now_add_peer esp_now_del_peer
    esp_now_register_send_cb esp_now_register_recv_cb
    esp_now_unregister_send_cb esp_now_unregister_recv_cb esp_now_set_pmk
    esp_wifi_init esp_wifi_set_storage esp_wifi_set_mode esp_wifi_start esp_wifi_set_channel
    esp_wifi_set_config esp_wifi_connect esp_netif_create_default_wifi_sta)
foreach(sym ${ESPNET_WRAPPED})
    target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=${sym}")
endforeach()
