# Firmware E: guest CPU throughput per unit of guest time, to measure barrier dilation (V-020).
# It sends no frames, so it needs no shim and builds the same for a board.
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cpu_probe)
