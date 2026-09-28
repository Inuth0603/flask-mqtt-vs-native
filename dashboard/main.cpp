#include <gtk/gtk.h>
#include <mqtt/async_client.h>
#include <iostream>
#include <fstream>
#include <string>
#include <sstream>
#include <mutex>
#include <atomic>
#include <chrono>
#include <vector>
#include <cstdlib>
#include <algorithm>
#include <numeric>
#include <csignal>

const std::string SERVER_ADDRESS { "tcp://localhost:1883" };
const std::string CLIENT_ID { "gtk4_dashboard_client" };
const std::string TOPIC { "test/topic" };

// ---------------------------------------------------------------------------
// Shared state between network thread and UI thread
// ---------------------------------------------------------------------------
std::mutex state_mutex;
std::string latest_message = "Waiting for data...";
bool has_new_message = false;

// ---------------------------------------------------------------------------
// Instrumentation: message counter & latency samples  (Concerns 1 & 4)
// ---------------------------------------------------------------------------
std::atomic<uint64_t> msg_count{0};

std::mutex latency_mutex;
std::vector<double> latency_samples_us;   // microseconds

// Reserve space so the vector doesn't re-allocate under load
static constexpr size_t LATENCY_RESERVE = 400000;

// ---------------------------------------------------------------------------
// Instrumentation: FPS measurement  (Concern 5)
// ---------------------------------------------------------------------------
static int    fps_frame_count = 0;
static gint64 fps_last_time   = 0;       // microseconds (g_get_monotonic_time)
static double measured_fps    = 0.0;

// ---------------------------------------------------------------------------
// Log directory (passed via env var BENCHMARK_LOG_DIR, default "benchmark_logs")
// ---------------------------------------------------------------------------
static std::string log_dir() {
    const char* d = std::getenv("BENCHMARK_LOG_DIR");
    return d ? std::string(d) : "benchmark_logs";
}

// ---------------------------------------------------------------------------
// Helper: read the monotonic nanosecond timestamp embedded by the publisher
//
// Payload format from revised flood_data.py:
//     "<monotonic_ns>|Data Packet #<n>..."
//
// We convert the embedded ns value to a timespec so we can compare it against
// our own CLOCK_MONOTONIC reading.
// ---------------------------------------------------------------------------
static double compute_latency_us(const std::string& payload) {
    auto sep = payload.find('|');
    if (sep == std::string::npos) return -1.0;

    uint64_t pub_ns = 0;
    try {
        pub_ns = std::stoull(payload.substr(0, sep));
    } catch (...) {
        return -1.0;
    }

    // Read our own monotonic clock in nanoseconds
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint64_t now_ns = static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL
                    + static_cast<uint64_t>(ts.tv_nsec);

    if (now_ns < pub_ns) return 0.0;   // clock skew guard (same host, shouldn't happen)
    return static_cast<double>(now_ns - pub_ns) / 1000.0;  // → microseconds
}

// ---------------------------------------------------------------------------
// GTK timer function executed on the main GUI thread (~60 Hz)
// ---------------------------------------------------------------------------
static gboolean update_label_timer(gpointer user_data) {
    GtkWidget* label = static_cast<GtkWidget*>(user_data);

    std::string text_to_display;
    bool should_update = false;

    // Safely lock the mutex to check for and grab the new message
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (has_new_message) {
            text_to_display = latest_message;
            has_new_message = false;
            should_update = true;
        }
    }

    // Only update the GTK UI if a new message actually arrived
    if (should_update) {
        gtk_label_set_text(GTK_LABEL(label), text_to_display.c_str());
    }

    // ---- FPS measurement (Concern 5) ----
    fps_frame_count++;
    gint64 now = g_get_monotonic_time();        // microseconds
    if (fps_last_time == 0) fps_last_time = now;
    double elapsed_s = (now - fps_last_time) / 1.0e6;
    if (elapsed_s >= 1.0) {
        measured_fps = fps_frame_count / elapsed_s;
        std::cout << "[FPS] Measured: " << measured_fps
                  << "  |  Messages received: " << msg_count.load()
                  << std::endl;
        fps_frame_count = 0;
        fps_last_time = now;
    }

    return G_SOURCE_CONTINUE; // keep the timer running
}

// ---------------------------------------------------------------------------
// MQTT callback class (Network Thread)
// ---------------------------------------------------------------------------
class mqtt_callback : public virtual mqtt::callback, public virtual mqtt::iaction_listener {
private:
    mqtt::async_client& cli_;

public:
    mqtt_callback(mqtt::async_client& cli) : cli_(cli) {}

    // iaction_listener callbacks for connection
    void on_failure(const mqtt::token& tok) override {
        std::cout << "Connection failed!" << std::endl;
        std::lock_guard<std::mutex> lock(state_mutex);
        latest_message = "Connection failed!";
        has_new_message = true;
    }

    void on_success(const mqtt::token& tok) override {
        std::cout << "Connected successfully! Subscribing to " << TOPIC << std::endl;
        cli_.subscribe(TOPIC, 0, nullptr, *this);

        std::lock_guard<std::mutex> lock(state_mutex);
        latest_message = "Connected! Waiting for data...";
        has_new_message = true;
    }

    // callback interface for messages
    void connection_lost(const std::string& cause) override {
        std::cout << "\nConnection lost" << std::endl;
    }

    void delivery_complete(mqtt::delivery_token_ptr tok) override {}

    void message_arrived(mqtt::const_message_ptr msg) override {
        // --- Concern 1: count every received message ---
        msg_count.fetch_add(1, std::memory_order_relaxed);

        const std::string& payload = msg->get_payload_str();

        // --- Concern 4: compute publish-to-receive latency ---
        double lat = compute_latency_us(payload);
        if (lat >= 0.0) {
            std::lock_guard<std::mutex> lk(latency_mutex);
            latency_samples_us.push_back(lat);
        }

        // Update shared display state (existing logic)
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            // Strip the timestamp prefix for display
            auto sep = payload.find('|');
            if (sep != std::string::npos)
                latest_message = "Received: " + payload.substr(sep + 1);
            else
                latest_message = "Received: " + payload;
            has_new_message = true;
        }
    }
};

// ---------------------------------------------------------------------------
// Dump instrumentation data on exit
// ---------------------------------------------------------------------------
static void dump_stats() {
    uint64_t total = msg_count.load();
    std::cout << "\n=== Native C++ Dashboard Stats ===" << std::endl;
    std::cout << "Total messages received: " << total << std::endl;
    std::cout << "Last measured FPS:       " << measured_fps << std::endl;

    // Latency statistics
    std::vector<double> samples;
    {
        std::lock_guard<std::mutex> lk(latency_mutex);
        samples = latency_samples_us;       // copy
    }

    if (!samples.empty()) {
        std::sort(samples.begin(), samples.end());
        size_t n = samples.size();
        double median = samples[n / 2];
        double p95    = samples[static_cast<size_t>(n * 0.95)];
        double p99    = samples[static_cast<size_t>(n * 0.99)];
        double avg    = std::accumulate(samples.begin(), samples.end(), 0.0) / n;

        std::cout << "Latency samples:         " << n << std::endl;
        std::cout << "  Mean:   " << avg    / 1000.0 << " ms" << std::endl;
        std::cout << "  Median: " << median / 1000.0 << " ms" << std::endl;
        std::cout << "  p95:    " << p95    / 1000.0 << " ms" << std::endl;
        std::cout << "  p99:    " << p99    / 1000.0 << " ms" << std::endl;

        // Write latency log file
        std::string dir = log_dir();
        // mkdir -p equivalent is not trivial in pure C++17; rely on benchmark harness
        std::string path = dir + "/native_latency.csv";
        std::ofstream ofs(path);
        if (ofs) {
            ofs << "latency_us\n";
            for (double v : samples) ofs << v << "\n";
            std::cout << "Latency log written to " << path << std::endl;
        }
    }

    // Write summary log
    {
        std::string dir = log_dir();
        std::string path = dir + "/native_summary.log";
        std::ofstream ofs(path);
        if (ofs) {
            ofs << "messages_received=" << total << "\n";
            ofs << "measured_fps=" << measured_fps << "\n";
            if (!samples.empty()) {
                std::sort(samples.begin(), samples.end());
                size_t n = samples.size();
                ofs << "latency_median_us=" << samples[n / 2] << "\n";
                ofs << "latency_p95_us="    << samples[static_cast<size_t>(n * 0.95)] << "\n";
                ofs << "latency_p99_us="    << samples[static_cast<size_t>(n * 0.99)] << "\n";
            }
        }
    }
}

// Global pointer to keep the callback alive
static mqtt_callback* cb = nullptr;

static void on_activate(GtkApplication *app, gpointer user_data) {
    GtkWidget *window = gtk_application_window_new(app);
    gtk_window_set_title(GTK_WINDOW(window), "Optimized Native Interface");
    gtk_window_set_default_size(GTK_WINDOW(window), 400, 300);

    GtkWidget *label = gtk_label_new("Connecting to MQTT Broker...");

    gtk_widget_set_valign(label, GTK_ALIGN_CENTER);
    gtk_widget_set_halign(label, GTK_ALIGN_CENTER);
    PangoAttrList *attrs = pango_attr_list_new();
    pango_attr_list_insert(attrs, pango_attr_scale_new(2.0));
    gtk_label_set_attributes(GTK_LABEL(label), attrs);
    pango_attr_list_unref(attrs);

    gtk_window_set_child(GTK_WINDOW(window), label);
    gtk_window_present(GTK_WINDOW(window));

    // Register a 60 FPS (approx 16ms) timer loop for the UI redraws
    g_timeout_add(16, update_label_timer, label);

    // Initialize MQTT Client
    mqtt::async_client* client = static_cast<mqtt::async_client*>(user_data);

    cb = new mqtt_callback(*client);
    client->set_callback(*cb);

    mqtt::connect_options connOpts;
    connOpts.set_clean_session(true);

    try {
        client->connect(connOpts, nullptr, *cb);
    } catch (const mqtt::exception& exc) {
        std::cerr << "Error: " << exc.what() << std::endl;
    }
}

int main(int argc, char **argv) {
    // Pre-allocate latency vector
    latency_samples_us.reserve(LATENCY_RESERVE);

    mqtt::async_client client(SERVER_ADDRESS, CLIENT_ID);

    GtkApplication *app = gtk_application_new("com.example.OptimizedDashboard", G_APPLICATION_DEFAULT_FLAGS);

    g_signal_connect(app, "activate", G_CALLBACK(on_activate), &client);

    int status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);

    // Dump stats before exit
    dump_stats();

    if (cb != nullptr) {
        delete cb;
    }

    return status;
}
