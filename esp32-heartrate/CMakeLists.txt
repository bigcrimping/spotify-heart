# ESP-IDF v5.2+ — native heart-rate reporter for the Seeed MR60BHA2
# (XIAO ESP32-C6 + 60 GHz radar). Replaces the Arduino HeartRateOnly sketch.
#   idf.py set-target esp32c6
#   idf.py build
#   idf.py -p COMx flash monitor
# See PLAN.md for the output contract and the phase checklist.
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(spotify_heart_hr)
