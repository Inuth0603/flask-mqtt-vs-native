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
#include <ctime>

const std::string SERVER_ADDRESS { "tcp://localhost:1883" };
const std::string CLIENT_ID { "gtk4_dashboard_client" };
const std::string TOPIC { "test/topic" };

// ---------------------------------------------------------------------------
// Shared state between network thread and UI thread
// ---------------------------------------------------------------------------
std::mutex state_mutex;
std::string latest_display_text = "Waiting for data...";
uint64_t    latest_mono_ns = 0;       // publisher's monotonic timestamp
bool has_new_message = false;

// ---------------------------------------------------------------------------
// Instrumentation: message counter  (Concern 1)
// ---------------------------------------------------------------------------
std::atomic<uint64_t> msg_count{0};

// ---------------------------------------------------------------------------
// Instrumentation: DISPLAY latency  (Concern 4 — measured at render time)
//
// Latency is computed in the UI timer callback (render stage), NOT in
// message_arrived(). This gives publish-to-display latency, which is what
// the reviewer asked for.
// ---------------------------------------------------------------------------
std::mutex latency_mutex;
std::vector<double> display_latency_us;   // microseconds

// ---------------------------------------------------------------------------
// Instrumentation: frame-time distribution  (Concern 5)
//
// Records the wall-clock time of each frame callback to compute inter-frame
// intervals. This lets us report frame-time distribution (mean, stdev, max)
// rather than just average FPS, which hides dropped frames.
// ---------------------------------------------------------------------------
std::mutex frame_mutex;
std::vector<double> frame_times_us;       // inter-frame intervals in µs
static gint64 prev_frame_time = 0;        // µs, from g_get_monotonic_time()
static int    fps_frame_count = 0;
static gint64 fps_last_time   = 0;
static double measured_fps    = 0.0;

// ---------------------------------------------------------------------------
// Log directory
// ---------------------------------------------------------------------------
static std::string log_dir() {
    const char* d = std::getenv("BENCHMARK_LOG_DIR");
    return d ? std::string(d) : "benchmark_logs";
}

// ---------------------------------------------------------------------------
// Parse the dual-timestamp payload from revised flood_data.py
//
// Format: "<monotonic_ns>:<wallclock_ns>|Data Packet #<n>..."
//
// Returns the monotonic_ns value (for CLOCK_MONOTONIC comparison).
// ---------------------------------------------------------------------------
static uint64_t parse_mono_ns(const std::string& payload) {
    auto colon = payload.find(':');
    if (colon == std::string::npos) return 0;
    try {
        return std::stoull(payload.substr(0, colon));
    } catch (...) {
        return 0;
    }
}

// Extract display text (everything after '|')
static std::string extract_display_text(const std::string& payload) {
    auto sep = payload.find('|');
    if (sep != std::string::npos)
        return "Received: " + payload.substr(sep + 1);
    return "Received: " + payload;
}

// ---------------------------------------------------------------------------
// GTK timer function — THE RENDER STAGE (~60 Hz)
//
// Latency is computed HERE, not in message_arrived(), because this is
// when the data actually becomes visible to the user.
// ---------------------------------------------------------------------------
static gboolean update_label_timer(gpointer user_data) {
    GtkWidget* label = static_cast<GtkWidget*>(user_data);
    gint64 now_us = g_get_monotonic_time();  // µs, CLOCK_MONOTONIC

    // ---- Frame-time distribution (Concern 5) ----
    if (prev_frame_time > 0) {
        double interval_us = static_cast<double>(now_us - prev_frame_time);
        std::lock_guard<std::mutex> lk(frame_mutex);
        frame_times_us.push_back(interval_us);
    }
    prev_frame_time = now_us;

    // ---- FPS counter ----
    fps_frame_count++;
    if (fps_last_time == 0) fps_last_time = now_us;
    double elapsed_s = (now_us - fps_last_time) / 1.0e6;
    if (elapsed_s >= 1.0) {
        measured_fps = fps_frame_count / elapsed_s;
        std::cout << "[FPS] Measured: " << measured_fps
                  << "  |  Messages received: " << msg_count.load()
                  << std::endl;
        fps_frame_count = 0;
        fps_last_time = now_us;
    }

    // ---- Read latest message and compute DISPLAY latency ----
    std::string text_to_display;
    bool should_update = false;
    uint64_t pub_mono_ns = 0;

    {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (has_new_message) {
            text_to_display = latest_display_text;
            pub_mono_ns = latest_mono_ns;
            has_new_message = false;
            should_update = true;
        }
    }

    if (should_update) {
        gtk_label_set_text(GTK_LABEL(label), text_to_display.c_str());

        // Compute publish-to-DISPLAY latency using CLOCK_MONOTONIC
        if (pub_mono_ns > 0) {
            struct timespec ts;
            clock_gettime(CLOCK_MONOTONIC, &ts);
            uint64_t render_ns = static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL
                               + static_cast<uint64_t>(ts.tv_nsec);
            if (render_ns >= pub_mono_ns) {
                double lat_us = static_cast<double>(render_ns - pub_mono_ns) / 1000.0;
                std::lock_guard<std::mutex> lk(latency_mutex);
                display_latency_us.push_back(lat_us);
            }
        }
    }

    return G_SOURCE_CONTINUE;
}

// ---------------------------------------------------------------------------
// MQTT callback class (Network Thread)
// ---------------------------------------------------------------------------
class mqtt_callback : public virtual mqtt::callback, public virtual mqtt::iaction_listener {
private:
    mqtt::async_client& cli_;

public:
    mqtt_callback(mqtt::async_client& cli) : cli_(cli) {}

    void on_failure(const mqtt::token& tok) override {
        std::cout << "Connection failed!" << std::endl;
        std::lock_guard<std::mutex> lock(state_mutex);
        latest_display_text = "Connection failed!";
        latest_mono_ns = 0;
        has_new_message = true;
    }

    void on_success(const mqtt::token& tok) override {
        std::cout << "Connected successfully! Subscribing to " << TOPIC << std::endl;
        cli_.subscribe(TOPIC, 0, nullptr, *this);

        std::lock_guard<std::mutex> lock(state_mutex);
        latest_display_text = "Connected! Waiting for data...";
        latest_mono_ns = 0;
        has_new_message = true;
    }

    void connection_lost(const std::string& cause) override {
        std::cout << "\nConnection lost" << std::endl;
    }

    void delivery_complete(mqtt::delivery_token_ptr tok) override {}

    void message_arrived(mqtt::const_message_ptr msg) override {
        // Count every received message (Concern 1)
        msg_count.fetch_add(1, std::memory_order_relaxed);

        const std::string& payload = msg->get_payload_str();

        // Store raw timestamp + display text for the render thread.
        // Latency is NOT computed here — it's computed in the UI callback
        // when the data is actually displayed.
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            latest_mono_ns = parse_mono_ns(payload);
            latest_display_text = extract_display_text(payload);
            has_new_message = true;
        }
    }
};

// ---------------------------------------------------------------------------
// Dump all instrumentation on exit
// ---------------------------------------------------------------------------
static void dump_stats() {
    uint64_t total = msg_count.load();
    std::cout << "\n=== Native C++ Dashboard Stats ===" << std::endl;
    std::cout << "Total messages received: " << total << std::endl;
    std::cout << "Last measured FPS:       " << measured_fps << std::endl;

    std::string dir = log_dir();
    // Note: the benchmark harness should mkdir -p this directory

    // ---- Display latency (Concern 4) ----
    std::vector<double> lat_samples;
    {
        std::lock_guard<std::mutex> lk(latency_mutex);
        lat_samples = display_latency_us;
    }

    if (!lat_samples.empty()) {
        std::sort(lat_samples.begin(), lat_samples.end());
        size_t n = lat_samples.size();
        double median = lat_samples[n / 2];
        double p95    = lat_samples[static_cast<size_t>(n * 0.95)];
        double p99    = lat_samples[static_cast<size_t>(n * 0.99)];
        double avg    = std::accumulate(lat_samples.begin(), lat_samples.end(), 0.0) / n;

        std::cout << "Display latency samples:  " << n << std::endl;
        std::cout << "  Mean:   " << avg    / 1000.0 << " ms" << std::endl;
        std::cout << "  Median: " << median / 1000.0 << " ms" << std::endl;
        std::cout << "  p95:    " << p95    / 1000.0 << " ms" << std::endl;
        std::cout << "  p99:    " << p99    / 1000.0 << " ms" << std::endl;

        std::string csv_path = dir + "/native_display_latency.csv";
        std::ofstream ofs(csv_path);
        if (ofs) {
            ofs << "latency_us\n";
            for (double v : lat_samples) ofs << v << "\n";
            std::cout << "Display latency log: " << csv_path << std::endl;
        }
    }

    // ---- Frame-time distribution (Concern 5) ----
    std::vector<double> ft_samples;
    {
        std::lock_guard<std::mutex> lk(frame_mutex);
        ft_samples = frame_times_us;
    }

    if (!ft_samples.empty()) {
        std::sort(ft_samples.begin(), ft_samples.end());
        size_t n = ft_samples.size();
        double avg = std::accumulate(ft_samples.begin(), ft_samples.end(), 0.0) / n;
        double min_ft = ft_samples.front();
        double max_ft = ft_samples.back();
        double p99 = ft_samples[static_cast<size_t>(n * 0.99)];

        // Standard deviation
        double sq_sum = 0;
        for (double v : ft_samples) sq_sum += (v - avg) * (v - avg);
        double stdev = std::sqrt(sq_sum / n);

        double avg_fps = 1.0e6 / avg;

        std::cout << "Frame-time distribution (" << n << " frames):" << std::endl;
        std::cout << "  Mean interval: " << avg / 1000.0 << " ms (≈" << avg_fps << " FPS)" << std::endl;
        std::cout << "  Stdev:         " << stdev / 1000.0 << " ms" << std::endl;
        std::cout << "  Min:           " << min_ft / 1000.0 << " ms" << std::endl;
        std::cout << "  Max:           " << max_ft / 1000.0 << " ms" << std::endl;
        std::cout << "  p99:           " << p99 / 1000.0 << " ms" << std::endl;

        // Count "dropped" frames (interval > 2× expected 16.67ms)
        int dropped = 0;
        for (double v : ft_samples) {
            if (v > 33333.0) dropped++;  // > 33.3ms = missed vsync
        }
        std::cout << "  Dropped frames (>33ms): " << dropped
                  << " (" << (100.0 * dropped / n) << "%)" << std::endl;

        std::string csv_path = dir + "/native_frame_times.csv";
        std::ofstream ofs(csv_path);
        if (ofs) {
            ofs << "frame_interval_us\n";
            for (double v : ft_samples) ofs << v << "\n";
            std::cout << "Frame-time log: " << csv_path << std::endl;
        }
    }

    // ---- Summary file ----
    {
        std::string path = dir + "/native_summary.log";
        std::ofstream ofs(path);
        if (ofs) {
            ofs << "messages_received=" << total << "\n";
            ofs << "measured_fps=" << measured_fps << "\n";
            if (!lat_samples.empty()) {
                size_t n = lat_samples.size();
                ofs << "display_latency_median_us=" << lat_samples[n / 2] << "\n";
                ofs << "display_latency_p95_us=" << lat_samples[static_cast<size_t>(n * 0.95)] << "\n";
                ofs << "display_latency_p99_us=" << lat_samples[static_cast<size_t>(n * 0.99)] << "\n";
            }
            if (!ft_samples.empty()) {
                size_t n = ft_samples.size();
                double avg = std::accumulate(ft_samples.begin(), ft_samples.end(), 0.0) / n;
                ofs << "frame_time_mean_us=" << avg << "\n";
                ofs << "frame_time_max_us=" << ft_samples.back() << "\n";
                int dropped = 0;
                for (double v : ft_samples) if (v > 33333.0) dropped++;
                ofs << "dropped_frames=" << dropped << "\n";
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

    // Register a ~60 Hz timer loop for UI redraws
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
    srand(static_cast<unsigned>(time(nullptr)));
    std::string unique_client_id = CLIENT_ID + "_" + std::to_string(rand());
    mqtt::async_client client(SERVER_ADDRESS, unique_client_id);

    GtkApplication *app = gtk_application_new("com.example.OptimizedDashboard", G_APPLICATION_DEFAULT_FLAGS);

    g_signal_connect(app, "activate", G_CALLBACK(on_activate), &client);

    int status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);

    dump_stats();

    if (cb != nullptr) {
        delete cb;
    }

    return status;
}
