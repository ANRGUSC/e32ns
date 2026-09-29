# espnet_node — proven emulation node (spike #2). Build on Linux + ESP-IDF v5.4.2:
#   idf.py set-target esp32s3 && idf.py build
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espnet_node)
