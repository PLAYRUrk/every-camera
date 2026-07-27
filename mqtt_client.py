"""
MQTT publisher and subscriber for console and GUI modes.
All MQTT operations are non-blocking and failure-safe.
"""
import os
import json
import socket
import uuid

import console_ui

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


def make_client_id(role):
    """Build a client id that is unique across machines.

    A plain ``{role}_{pid}`` is not: two nodes easily end up with the same PID,
    and an MQTT broker kicks off whichever client already holds the id — so
    both would reconnect in a loop and lose commands.
    """
    try:
        host = socket.gethostname().split(".")[0][:20]
    except Exception:
        host = "host"
    return f"{role}_{host}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _make_mqtt_client(client_id):
    """Create paho-mqtt Client compatible with both v1 and v2 API."""
    try:
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
    except AttributeError:
        return mqtt.Client(client_id=client_id)


def offline_payload(instance_name, camera_type):
    """Last-will payload: what the broker publishes if this node vanishes."""
    return json.dumps({
        "instance_name": instance_name,
        "camera_type": camera_type,
        "status": "offline",
        "note": "connection lost (MQTT last will)",
    })


# ---------------------------------------------------------------------------
# Console MQTT publisher (no Qt — safe to use in threads)
# ---------------------------------------------------------------------------
class MqttPublisherConsole:
    """Minimal paho-mqtt wrapper for console mode (no Qt signals)."""

    def __init__(self, host, port, user="", password="", use_tls=False,
                 client_id="", will_topic=None, will_payload=None):
        if not MQTT_AVAILABLE:
            raise RuntimeError("paho-mqtt not installed: pip install paho-mqtt")
        self._client = _make_mqtt_client(client_id or make_client_id("ecam_pub"))
        if user:
            self._client.username_pw_set(user, password)
        if use_tls:
            self._client.tls_set()
        if will_topic:
            # Retained, so a monitor connecting later still sees that this node
            # went away instead of its last "running" status.
            self._client.will_set(will_topic, will_payload or "",
                                  qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._host = host
        self._port = int(port)
        self._sub_topic = None
        self._on_command_cb = None
        self._check_timer = None

    def connect_broker(self):
        console_ui.log(f"MQTT connecting to {self._host}:{self._port}...")
        try:
            self._client.reconnect_delay_set(min_delay=2, max_delay=30)
            self._client.connect_async(self._host, self._port, keepalive=120)
            self._client.loop_start()
        except Exception as exc:
            console_ui.warn(f"MQTT connect error: {exc}")
            return
        # Async — schedule a diagnostic check after a few seconds.
        import threading as _th

        def _check_connected():
            if self._client.is_connected():
                return
            console_ui.warn(f"MQTT still NOT connected to {self._host}:{self._port} "
                            f"after 3s. Check network/firewall/credentials. "
                            f"Commands from monitor will not be received.")
        self._check_timer = _th.Timer(3.0, _check_connected)
        self._check_timer.daemon = True
        self._check_timer.start()

    def disconnect_broker(self):
        if self._check_timer is not None:
            self._check_timer.cancel()
            self._check_timer = None
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

    def clear_retained(self, topic):
        """Erase a retained topic by publishing an empty retained payload.

        Without this a stopped camera keeps its last retained status on the
        broker forever and shows up as a live instance in every monitor.
        """
        try:
            info = self._client.publish(topic, b"", qos=1, retain=True)
            # Give the broker a moment before the caller disconnects.
            try:
                info.wait_for_publish(timeout=3.0)
            except (AttributeError, TypeError, ValueError):
                pass
        except Exception:
            pass

    def subscribe_commands(self, topic, callback):
        """Subscribe to command topic. callback(topic, payload_bytes)."""
        self._sub_topic = topic
        self._on_command_cb = callback
        if self._client.is_connected():
            self._client.subscribe(self._sub_topic, qos=1)
            console_ui.log(f"MQTT subscribed: {topic}")
        else:
            console_ui.log(f"MQTT subscription pending (not connected yet): {topic}")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            console_ui.log("MQTT connected")
            if self._sub_topic:
                client.subscribe(self._sub_topic, qos=1)
                console_ui.log(f"MQTT subscribed on connect: {self._sub_topic}")
        else:
            console_ui.warn(f"MQTT connection refused (rc={reason_code})")

    def _on_disconnect(self, client, userdata, disconnect_flags=None,
                       reason_code=None, properties=None):
        console_ui.warn(f"MQTT disconnected (rc={reason_code})")

    def _on_message(self, client, userdata, msg):
        console_ui.log(f"MQTT message received: {msg.topic} "
                       f"({len(msg.payload)} bytes)")
        if self._on_command_cb:
            try:
                self._on_command_cb(msg.topic, msg.payload)
            except Exception as e:
                console_ui.error(f"MQTT callback error: {e}")
        else:
            console_ui.warn("MQTT message received but no callback registered")


# ---------------------------------------------------------------------------
# Qt MQTT publisher (for GUI mode)
# ---------------------------------------------------------------------------
try:
    from PyQt5.QtCore import QObject, pyqtSignal

    class MqttPublisher(QObject):
        connected = pyqtSignal()
        disconnected = pyqtSignal()
        error = pyqtSignal(str)
        command_received = pyqtSignal(str, bytes)  # topic, payload

        def __init__(self, host, port, user="", password="", use_tls=False,
                     client_id="", will_topic=None, will_payload=None):
            super().__init__()
            if not MQTT_AVAILABLE:
                raise RuntimeError("paho-mqtt not installed: pip install paho-mqtt")
            self._client = _make_mqtt_client(client_id or make_client_id("ecam_pub"))
            if user:
                self._client.username_pw_set(user, password)
            if use_tls:
                self._client.tls_set()
            if will_topic:
                self._client.will_set(will_topic, will_payload or "",
                                      qos=1, retain=True)
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            self._host = host
            self._port = int(port)
            self._sub_topic = None
            self._stopping = False

        def connect_broker(self):
            self._stopping = False
            try:
                self._client.reconnect_delay_set(min_delay=2, max_delay=30)
                self._client.connect_async(self._host, self._port, keepalive=120)
                self._client.loop_start()
            except Exception as e:
                self.error.emit(str(e))

        def disconnect_broker(self):
            self._stopping = True
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

        def clear_retained(self, topic):
            """Erase a retained topic (see MqttPublisherConsole.clear_retained)."""
            try:
                info = self._client.publish(topic, b"", qos=1, retain=True)
                try:
                    info.wait_for_publish(timeout=3.0)
                except (AttributeError, TypeError, ValueError):
                    pass
            except Exception:
                pass

        def subscribe_commands(self, topic):
            """Subscribe to command topic. Emits command_received signal."""
            self._sub_topic = topic
            if self._client.is_connected():
                self._client.subscribe(self._sub_topic, qos=1)

        def _on_connect(self, client, userdata, flags, reason_code, properties=None):
            if reason_code == 0:
                self.connected.emit()
                if self._sub_topic:
                    client.subscribe(self._sub_topic, qos=1)
            else:
                self.error.emit(f"MQTT connection refused (rc={reason_code})")

        def _on_disconnect(self, client, userdata, disconnect_flags=None,
                           reason_code=None, properties=None):
            if self._stopping:
                self.disconnected.emit()

        def _on_message(self, client, userdata, msg):
            try:
                self.command_received.emit(msg.topic, bytes(msg.payload))
            except Exception:
                pass

    class MqttSubscriber(QObject):
        """MQTT subscriber for monitor GUI."""
        connected = pyqtSignal()
        disconnected = pyqtSignal()
        message_received = pyqtSignal(str, str)  # topic, payload JSON
        binary_received = pyqtSignal(str, bytes)  # topic, raw payload
        error = pyqtSignal(str)

        def __init__(self, host, port, user="", password="", use_tls=False, client_id=""):
            super().__init__()
            if not MQTT_AVAILABLE:
                raise RuntimeError("paho-mqtt not installed: pip install paho-mqtt")
            self._client = _make_mqtt_client(client_id or make_client_id("ecam_mon"))
            if user:
                self._client.username_pw_set(user, password)
            if use_tls:
                self._client.tls_set()
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            self._host = host
            self._port = int(port)
            self._topics = []
            self._stopping = False

        def connect_broker(self, topic):
            self._stopping = False
            self._topics = [topic] if isinstance(topic, str) else list(topic)
            try:
                self._client.reconnect_delay_set(min_delay=2, max_delay=30)
                self._client.connect_async(self._host, self._port, keepalive=120)
                self._client.loop_start()
            except Exception as e:
                self.error.emit(str(e))

        def add_subscription(self, topic):
            """Add extra subscription after connection."""
            if topic not in self._topics:
                self._topics.append(topic)
            if self._client.is_connected():
                self._client.subscribe(topic, qos=1)

        def publish(self, topic, payload, retain=False, qos=1):
            """Publish a message (for sending commands)."""
            try:
                self._client.publish(topic, payload, qos=qos, retain=retain)
            except Exception:
                pass

        def disconnect_broker(self):
            self._stopping = True
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

        def _on_connect(self, client, userdata, flags, reason_code, properties=None):
            if reason_code == 0:
                for t in self._topics:
                    client.subscribe(t, qos=1)
                self.connected.emit()
            else:
                self.error.emit(f"Connection refused (rc={reason_code})")

        def _on_disconnect(self, client, userdata, disconnect_flags=None,
                           reason_code=None, properties=None):
            if self._stopping:
                self.disconnected.emit()

        def _on_message(self, client, userdata, msg):
            # Text where possible (all every-camera payloads are JSON), binary
            # only as a fallback — emitting both copied every frame twice.
            try:
                self.message_received.emit(msg.topic, msg.payload.decode("utf-8"))
                return
            except UnicodeDecodeError:
                pass
            except Exception:
                return
            try:
                self.binary_received.emit(msg.topic, bytes(msg.payload))
            except Exception:
                pass

except ImportError:
    # PyQt5 not available — only console classes usable
    pass


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------
def create_console_publisher(mqtt_cfg, instance_name=None, camera_type=None):
    """Create MqttPublisherConsole from config dict. Returns None on failure.

    When ``instance_name`` is given, a retained last-will is registered on the
    instance's status topic so a crashed node is reported as ``offline``
    immediately instead of lingering as "running" until it goes stale.
    """
    if not mqtt_cfg.get("enabled") or not MQTT_AVAILABLE:
        if mqtt_cfg.get("enabled") and not MQTT_AVAILABLE:
            console_ui.warn("MQTT disabled: install paho-mqtt")
        return None
    will_topic = will_payload = None
    if instance_name:
        prefix = mqtt_cfg.get("prefix", "every_camera")
        will_topic = f"{prefix}/{instance_name}/status"
        will_payload = offline_payload(instance_name, camera_type or "unknown")
    try:
        pub = MqttPublisherConsole(
            host=mqtt_cfg.get("host", "broker.hivemq.com"),
            port=mqtt_cfg.get("port", 1883),
            user=mqtt_cfg.get("user", ""),
            password=mqtt_cfg.get("password", ""),
            use_tls=mqtt_cfg.get("tls", False),
            will_topic=will_topic,
            will_payload=will_payload,
        )
        pub.connect_broker()
        return pub
    except Exception as exc:
        console_ui.warn(f"MQTT setup failed: {exc}")
        return None
