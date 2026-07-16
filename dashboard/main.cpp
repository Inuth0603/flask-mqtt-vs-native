#include <gtk/gtk.h>
#include <mqtt/async_client.h>
#include <iostream>
#include <string>
#include <mutex>

const std::string SERVER_ADDRESS { "tcp://localhost:1883" };
const std::string CLIENT_ID { "gtk4_dashboard_client" };
const std::string TOPIC { "test/topic" };

// Shared state between network thread and UI thread
std::mutex state_mutex;
std::string latest_message = "Waiting for data...";
bool has_new_message = false;

// GTK timer function executed on the main GUI thread at 60 FPS
static gboolean update_label_timer(gpointer user_data) {
    GtkWidget* label = static_cast<GtkWidget*>(user_data);
    
    std::string text_to_display;
    bool should_update = false;
    
    // Safely lock the mutex to check for and grab the new message
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (has_new_message) {
            text_to_display = latest_message;
            has_new_message = false; // reset the flag
            should_update = true;
        }
    }
    
    // Only update the GTK UI if a new message actually arrived
    if (should_update) {
        gtk_label_set_text(GTK_LABEL(label), text_to_display.c_str());
    }
    
    return G_SOURCE_CONTINUE; // keep the timer running
}

// MQTT callback class (Network Thread)
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
        // High-frequency callback! We ONLY update the string in memory.
        // We DO NOT trigger a GTK redraw here!
        std::lock_guard<std::mutex> lock(state_mutex);
        latest_message = "Received: " + msg->get_payload_str();
        has_new_message = true;
    }
};

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
    mqtt::async_client client(SERVER_ADDRESS, CLIENT_ID);

    GtkApplication *app = gtk_application_new("com.example.OptimizedDashboard", G_APPLICATION_DEFAULT_FLAGS);
    
    g_signal_connect(app, "activate", G_CALLBACK(on_activate), &client);
    
    int status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);
    
    if (cb != nullptr) {
        delete cb;
    }

    return status;
}
