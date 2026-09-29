# espnet_shim ESP-IDF component (D-017).
#
# PRIORITY_OVERRIDE note: this component provides the esp_now_* symbols. To make the
# app link against these rather than IDF's real esp_now, either (a) do not add the
# `esp_wifi` esp_now objects to the link (this component supplies them), or (b) use
# the --wrap linker approach documented in docs/QEMU_INTEGRATION.md (brief §9 risk #3).
idf_component_register(
    SRCS
        "espnet_shim.c"
        "espnet_transport_socket.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        esp_wifi      # for esp_now.h types (esp_now_peer_info_t, callbacks)
        esp_timer     # esp_timer_get_time() -> virtual-time TX stamp (D-022)
        lwip
        freertos
        esp_common
        log
)

# REQUIRED for interposition (risk #3, empirically confirmed — see RESEARCH_LOG.md D-021):
# component-provided strong symbols do NOT override IDF's esp_now_* (link order favors IDF's
# archive; the app called the real esp_now_init and crashed). --wrap forces every call site to
# our __wrap_esp_now_* instead. The shim's functions are named __wrap_esp_now_* to match.
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=esp_now_init"
    "-Wl,--wrap=esp_now_deinit"
    "-Wl,--wrap=esp_now_send"
    "-Wl,--wrap=esp_now_add_peer"
    "-Wl,--wrap=esp_now_del_peer"
    "-Wl,--wrap=esp_now_register_send_cb"
    "-Wl,--wrap=esp_now_register_recv_cb"
    "-Wl,--wrap=esp_now_unregister_send_cb"
    "-Wl,--wrap=esp_now_unregister_recv_cb"
    "-Wl,--wrap=esp_now_set_pmk")
