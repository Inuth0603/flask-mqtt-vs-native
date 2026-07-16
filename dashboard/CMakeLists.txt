cmake_minimum_required(VERSION 3.10)
project(MQTTDashboard)

set(CMAKE_CXX_STANDARD 17)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -DNDEBUG")

# Find Threads
find_package(Threads REQUIRED)

# Find GTK4
find_package(PkgConfig REQUIRED)
pkg_check_modules(GTK4 REQUIRED gtk4)

# Find Paho MQTT C and C++ libraries
find_library(PAHO_C paho-mqtt3as)
find_library(PAHO_CPP paho-mqttpp3)

include_directories(${GTK4_INCLUDE_DIRS})
link_directories(${GTK4_LIBRARY_DIRS})

add_executable(dashboard main.cpp)

# Link GTK4, Paho MQTT, and Threads
target_link_libraries(dashboard 
    ${GTK4_LIBRARIES} 
    ${PAHO_CPP} 
    ${PAHO_C}
    Threads::Threads
)
