# Same source builds for hardware (idf.py -DESPNET_HARDWARE=1 build) or for esp-netsim,
# where the espnet_shim component replaces the radio.
cmake_minimum_required(VERSION 3.16)
if(NOT ESPNET_HARDWARE)
    set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../components")
endif()
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espnow_txprobe)
