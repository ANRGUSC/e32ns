idf_component_register(SRCS "main.c" "../../espnow_common.c" INCLUDE_DIRS "." "../..")
# idf.py -DPING_LEN=250 build pads each ping to that many bytes; pong echoes the length back.
if(PING_LEN)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE PING_LEN=${PING_LEN})
endif()
