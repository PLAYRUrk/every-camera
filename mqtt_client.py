"""
MQTT publisher and subscriber for console and GUI modes.
All MQTT operations are non-blocking and failure-safe.
"""
import os
import json

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


def _make_mqtt_client(client_id):
    """Create paho-mqtt Client compatible with both v1 and v2 API."""
    try:
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
    except AttributeError:
        return mqtt.Client(client_id=client_id)


# ---------------------------------------------------------------------------
# Console MQTT publisher (no Qt — safe to use in threads)
# ---------------------------------------------------------------------------
class MqttPublisherConsole:
    """Minimal paho-mqtt wrapper for console mode (no Qt signals)."""

    def __init__(self, host, port, user="", password="", use_tls=False, client_id=""):
        if not MQTT_AVAILABLE:
            raise RuntimeError("paho-mqtt not installed: pip install paho-mqtt")
        self._client = _make_mqtt_client(client_id or f"ecam_pub_{os.getpid()}")
        if user:
            self._client.username_pw_set(user, password)
        if use_tls:
            self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._host = host
        self._port = int(port)

    def connect_broker(self):
        try:
            self._client.reconnect_delay_set(min_delay=2, max_delay=30)
            self._client.connect_async(self._host, self._port, keepalive=60)
            self._client.loop_start()
        except Exception as exc:
            print(f"[WARN] MQTT connect error: {exc}")

    def disconnect_broker(self):
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def publish(self, topic, payload, retain=True, qos=1):
        try:
            self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            print("[INFO] MQTT connected")
        else:
            print(f"[WARN] MQTT connection refused (rc={reason_code})")

    def _on_disconnect(self, client, userdata, disconnect_flags=None,
                       reason_code=None, properties=None):
        pass  # auto-reconnect handles this


# ---------------------------------------------------------------------------
# Qt MQTT publisher (for GUI mode)
# ---------------------------------------------------------------------------
try:
    from PyQt5.QtCore import QObject, pyqtSignal

    class MqttPublisher(QObject):
        connected = pyqtSignal()
        disconnected = pyqtSignal()
        error = pyqtSignal(str)

        def __init__(self, host, port, user="", password="", use_tls=False, client_id=""):
            super().__init__()
            if not MQTT_AVAILABLE:
                raise RuntimeError("paho-mqtt not installed: pip install paho-mqtt")
            self._client = _make_mqtt_client(client_id or f"ecam_pub_{os.getpid()}")
            if user:
                self._client.username_pw_set(user, password)
            if use_tls:
                self._client.tls_set()
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._host = host
            self._port = int(port)

        def connect_broker(self):
            try:
                self._client.reconnect_delay_set(min_delay=2, max_delay=30)
                self._client.connect_async(self._host, self._port, keepalive=60)
                self._client.loop_start()
            except Exception as e:
                self.error.emit(str(e))

        def disconnect_broker(self):
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

        def publish(self, topic, payload, retain=True, qos=1):
            try:
                self._client.publish(topic, payload, qos=qos, retain=retain)
            except Exception:
                pass

        def _on_connect(self, client, userdata, flags, reason_code, properties=None):
            if reason_code == 0:
                self.connected.emit()
            else:
                self.error.emit(f"MQTT connection refused (rc={reason_code})")

        def _on_disconnect(self, client, userdata, disconnect_flags=None,
                           reason_code=None, properties=None):
            self.disconnected.emit()

    class MqttSubscriber(QObject):
        """MQTT subscriber for monitor GUI."""
        connected = pyqtSignal()
        disconnected = pyqtSignal()
        message_received = pyqtSignal(str, str)  # topic, payload JSON
        error = pyqtSignal(str)

        def __init__(self, host, port, user="", password="", use_tls=False, client_id=""):
            super().__init__()
            if not MQTT_AVAILABLE:
                raise RuntimeError("paho-mqtt not installed: pip install paho-mqtt")
            self._client = _make_mqtt_client(client_id or f"ecam_mon_{os.getpid()}")
            if user:
                self._client.username_pw_set(user, password)
            if use_tls:
                self._client.tls_set()
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            self._host = host
            self._port = int(port)
            self._topic = None

        def connect_broker(self, topic):
            self._topic = topic
            try:
                self._client.reconnect_delay_set(min_delay=2, max_delay=30)
                self._client.connect_async(self._host, self._port, keepalive=60)
                self._client.loop_start()
            except Exception as e:
                self.error.emit(str(e))

        def disconnect_broker(self):
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

        def _on_connect(self, client, userdata, flags, reason_code, properties=None):
            if reason_code == 0:
                client.subscribe(self._topic, qos=1)
                self.connected.emit()
            else:
                self.error.emit(f"Connection refused (rc={reason_code})")

        def _on_disconnect(self, client, userdata, disconnect_flags=None,
                           reason_code=None, properties=None):
            self.disconnected.emit()

        def _on_message(self, client, userdata, msg):
            try:
                self.message_received.emit(msg.topic, msg.payload.decode("utf-8"))
            except Exception:
                pass

except ImportError:
    # PyQt5 not available — only console classes usable
    pass


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------
def create_console_publisher(mqtt_cfg):
    """Create MqttPublisherConsole from config dict. Returns None on failure."""
    if not mqtt_cfg.get("enabled") or not MQTT_AVAILABLE:
        if mqtt_cfg.get("enabled") and not MQTT_AVAILABLE:
            print("[WARN] MQTT disabled: install paho-mqtt")
        return None
    try:
        pub = MqttPublisherConsole(
            host=mqtt_cfg.get("host", "broker.hivemq.com"),
            port=mqtt_cfg.get("port", 1883),
            user=mqtt_cfg.get("user", ""),
            password=mqtt_cfg.get("password", ""),
            use_tls=mqtt_cfg.get("tls", False),
        )
        pub.connect_broker()
        return pub
    except Exception as exc:
        print(f"[WARN] MQTT setup failed: {exc}")
        return None
