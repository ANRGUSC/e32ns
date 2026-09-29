# Top-level ESP-IDF project for the espnow_ping example.
# Build (on Linux with ESP-IDF): idf.py set-target esp32s3 && idf.py build
# The espnet_shim component is picked up via EXTRA_COMPONENT_DIRS below.
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espnow_ping)
