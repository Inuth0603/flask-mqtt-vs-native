# Native C++ vs Full-Stack Web Telemetry Benchmark

This repository contains all the source code, benchmark scripts, and implementation architectures discussed in the paper:
**"High-Performance Telemetry Visualization: Native C++ Architectures vs. Standard Web-Stack Baselines in Factory Workstations"**.

## Repository Structure

- `dashboard/`: The highly optimized Native C++ GTK4 dashboard source code (`main.cpp` and `CMakeLists.txt`).
- `node_backend/`: The Full-Stack Optimized Web architecture using Node.js, `ws` WebSockets, and HTML5 Canvas.
- `example/`: The original naive Python Flask-SocketIO baseline and its Canvas-optimized variant.
- `flood_data.py`: The Python MQTT publisher script used to generate the 5,000 msg/sec data flood for the $N=10$ trials.

*(Note: This repository was originally forked from the `Flask-MQTT` library to provide the underlying Python baseline, but has been heavily modified to include the native C++ and Node.js architectures for the comparative study.)*

## How to Run the Benchmarks

Before running the Python backend or publisher, install the exact dependencies with `pip install -r requirements.txt`. (These dependencies are strictly pinned to the versions used in the reported trials — this is critical, as Section 3.6 of the paper documents version-sensitive async-worker behavior and deadlocks).

See the methodology section in the paper for further instructions on how to compile the C++ application using CMake and GTK4, run the `flood_data.py` publisher, and start the respective Node.js or Python backend servers.
