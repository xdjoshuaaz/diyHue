from concurrent import futures
import json
import logging
import random
import socket
import time
import traceback
from threading import Thread, current_thread
from functools import partial
from collections import namedtuple
import asyncio

from functions import light_types, nextFreeId
from functions.colors import convert_rgb_xy, convert_xy
from HueEmulator3 import getIpAddress

COLOR_FLOW_TIME = 30
MINIMUM_MSEC_BETWEEN_COMMANDS = 300
MINIMUM_MSEC_BETWEEN_COMMANDS_MUSIC = 30
TRANSITION = ["sudden", 30]

music_mode_connections = {}
socket_connections = {}
socket_connection_attempts = {}
queued_set_light_data = {}

event_loop = asyncio.new_event_loop()
Thread(name="yeelight-command-queue", target=event_loop.run_forever).start()


class SocketConnection():
    def __init__(self, ip, socket, mode='socket'):
        self.ip = ip
        self.socket = socket
        self.mode = mode

        self._pending_commands = {}
        self._next_command_id = 1
        self._active_thread = 0
        self._id = '{0}:{1} <- {2} -> {3}:{4}'.format(
            *list(self.socket.getsockname()), self.mode, *list(self.socket.getpeername()))

        logging.info(
            "%s: New yeelight socket", self._id)

    def start(self, threaded_recv=True):
        logging.info(
            "%s: Starting yeelight receive loop, threaded: %s", self._id, threaded_recv)
        if threaded_recv:
            Thread(target=self.recv_loop, name="yeelight-recv-" +
                   self.ip + "#" + str(self._active_thread)).start()
        else:
            self.recv_loop()

    def send(self, *args, **kwargs):
        try:
            logging.debug("%s: Send %s", self._id, args[0].decode('utf8'))
            return self.socket.sendall(*args, **kwargs)
        except Exception as ex:
            logging.exception("%s: Send exception", self._id)
            self.dispose()
            raise

    def recv_loop(self, *args, **kwargs):
        this_thread = self._active_thread + 1
        self._active_thread = this_thread
        logging.info(
            "%s: Started yeelight receive loop, thread id %d", self._id, this_thread)

        while self._active_thread == this_thread:
            try:
                logging.debug("%s: Receiving...", self._id)
                responses = self.socket.recv(16 * 1024)
                logging.debug("%s: Received", self._id)
            except Exception as ex:
                logging.exception("%s: Receive exception", self._id)
                self.dispose()
                raise

            if responses == b'':
                logging.info("%s: Received EOF. Socket has closed.", self._id)
                self._cancel_pending_command_invocations_with_exception(None)
                break

            response_list = responses.splitlines()
            for response in response_list:
                r = response.decode("utf8")
                j = json.loads(r)
                logging.debug("%s: Handling response: %s",
                              self._id, json.dumps(r))

                if "id" in j:
                    self._set_command_invocation_result(int(j["id"]), j)

    def _cancel_pending_command_invocations_with_exception(self, ex):
        pending_commands = self._pending_commands
        self._pending_commands = {}

        for (command_id, future) in pending_commands.items():
            logging.debug(
                "%s: Set exception on future for command id %d", self._id, command_id)
            future.set_exception(ex)

    def _set_command_invocation_result(self, command_id, *args):
        if command_id in self._pending_commands:
            future = self._pending_commands[command_id]
            del self._pending_commands[command_id]
            logging.debug("%s: Response belongs to command id %d, setting result of future",
                          self._id, command_id)
            future.set_result(*args)

    def invoke_command(self, method_name, *params):
        command_id = self._next_command_id
        self._next_command_id += 1

        future = futures.Future()
        future.set_running_or_notify_cancel()
        self._pending_commands[command_id] = future

        logging.debug("%s: Invoking command id %d, method %s, params %s",
                      self._id, command_id, method_name, json.dumps(params))

        def done_callback(f):
            logging.debug("%s: Future for command id %d is done",
                          self._id, command_id)
            if command_id in self._pending_commands:
                del self._pending_commands[command_id]

        future.add_done_callback(done_callback)

        self.send((json.dumps(
            {"id": command_id, "method": method_name, "params": params}) + "\r\n").encode())

        return future

    def dispose(self):
        logging.info("%s: Yeelight socket disposed", self._id)

        self.socket.close()

        if self.mode == "socket" and self.ip in socket_connections:
            del socket_connections[self.ip]

        if self.mode == "socket" and self.ip in socket_connection_attempts:
            del socket_connection_attempts[self.ip]


class MusicModeSocketConnection(SocketConnection):
    def __init__(self, ip, request):
        super().__init__(ip, request.connection, mode='music')
        self.request = request
        self.socket.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def start(self):
        current_thread().name = "yeelight-recv-music-" + \
            self.ip + "#" + str(self._active_thread)
        super().start(threaded_recv=False)

    def dispose(self):
        self.request.close_connection = True
        super().dispose()
        self.request.finish(force=True)

        if self.ip in music_mode_connections:
            del music_mode_connections[self.ip]


def discover(bridge_config, new_lights):
    group = ("239.255.255.250", 1982)
    message = "\r\n".join([
        'M-SEARCH * HTTP/1.1',
        'HOST: 239.255.255.250:1982',
        'MAN: "ssdp:discover"',
        'ST: wifi_bulb'])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(3)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.sendto(message.encode(), group)
    while True:
        try:
            response = sock.recv(1024).decode('utf-8').split("\r\n")
            properties = {"rgb": False, "ct": False}
            for line in response:
                if line[:2] == "id":
                    properties["id"] = line[4:]
                elif line[:3] == "rgb":
                    properties["rgb"] = True
                elif line[:2] == "ct":
                    properties["ct"] = True
                elif line[:8] == "Location":
                    properties["ip"] = line.split(":")[2][2:]
                elif line[:4] == "name":
                    properties["name"] = line[6:]
            device_exist = False
            for light in bridge_config["lights_address"].keys():
                if bridge_config["lights_address"][light]["protocol"] == "yeelight" and bridge_config["lights_address"][light]["id"] == properties["id"]:
                    device_exist = True
                    bridge_config["lights_address"][light]["ip"] = properties["ip"]
                    logging.debug(
                        "light id " + properties["id"] + " already exist, updating ip...")
                    break
            if (not device_exist):
                light_name = "YeeLight id " + \
                    properties["id"][-8:] if properties["name"] == "" else properties["name"]
                logging.debug("Add YeeLight: " + properties["id"])
                modelid = "LWB010"
                if properties["rgb"]:
                    modelid = "LCT015"
                elif properties["ct"]:
                    modelid = "LTW001"
                new_light_id = nextFreeId(bridge_config, "lights")
                bridge_config["lights"][new_light_id] = {"state": light_types[modelid]["state"], "type": light_types[modelid]["type"], "name": light_name, "uniqueid": "4a:e0:ad:7f:cf:" + str(
                    random.randrange(0, 99)) + "-1", "modelid": modelid, "manufacturername": "Philips", "swversion": light_types[modelid]["swversion"]}
                new_lights.update({new_light_id: {"name": light_name}})
                bridge_config["lights_address"][new_light_id] = {
                    "ip": properties["ip"], "id": properties["id"], "protocol": "yeelight"}

        except socket.timeout:
            logging.debug('Yeelight search end')
            sock.close()
            break


def handle_request(request_handler):
    # Bulb is creating a TCP socket with the server
    ip = request_handler.client_address[0]

    logging.info("%s incoming connection by music mode", ip)
    if ip in music_mode_connections:
        logging.info(
            "%s already has music mode connection, is trying to connect again", ip)
        music_mode_connections[ip].dispose()

    music_mode_connections[ip] = MusicModeSocketConnection(ip, request_handler)
    music_mode_connections[ip].start()

    return False


def new_connection(ip):
    while ip in socket_connection_attempts:
        logging.debug(
            "New connection method blocked until previous attempt finished for %s", ip)
        time.sleep(1)

    if ip in socket_connections:
        logging.info(
            "New connection method answered with recently created socket for %s", ip)
        return socket_connections[ip]

    logging.info("Creating new socket to %s", ip)

    socket_connection_attempts[ip] = True

    try:
        tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_socket.settimeout(None)
        tcp_socket.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        tcp_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp_socket.connect((ip, int(55443)))
        connection = SocketConnection(ip, tcp_socket)
        connection.start()

        socket_connections[ip] = connection
        if ip in socket_connection_attempts:
            del socket_connection_attempts[ip]

        set_music_on(connection)
    except Exception as ex:
        logging.warn("Connection to %s failed: ", json.dumps(ex))
        connection.dispose()
        raise

    return connection


def set_music_on(connection):
    try:
        server_ip = getIpAddress()
        logging.info("Setting music on for %s (to %s)",
                     connection.ip, server_ip)
        data = connection.invoke_command(
            "set_music", 1, server_ip, 80).result(5)

        logging.info("Set music on for %s returned %s",
                     connection.ip, json.dumps(data["result"]))

    except TimeoutError as ex:
        logging.info("Setting music on for %s failed %s",
                     connection.ip, json.dumps(ex))


def get_existing_connection(ip, music=True, socket=True, log=False):
    if ip in music_mode_connections and music:
        if log:
            logging.debug("Fetched music connection for %s", ip)
        return music_mode_connections[ip]
    elif ip in socket_connections and socket:
        if log:
            logging.debug("Fetched socket connection for %s", ip)
        return socket_connections[ip]
    else:
        if log:
            logging.debug("No connections for %s", ip)
        return None


def get_or_create_connection(ip, music=True, socket=True):
    connection = get_existing_connection(ip, music, socket, log=True)

    if connection is not None:
        return connection

    return new_connection(ip)


def minimum_msec_between_commands_for_ip(ip):
    connection = get_existing_connection(ip, log=False)

    if connection is not None and connection.mode == 'music':
        return MINIMUM_MSEC_BETWEEN_COMMANDS_MUSIC

    return MINIMUM_MSEC_BETWEEN_COMMANDS


def queue_set_light_data(ip, light, data):
    if ip not in queued_set_light_data:
        queued_set_light_data[ip] = {
            "timestamp": None,
            "data": {},
            "timer": None
        }

    if data is not None:
        queued_set_light_data[ip]["data"].update(data)

    if queued_set_light_data[ip]["timer"] is None:
        callback = partial(do_set_light_data, ip, light,
                           queued_set_light_data[ip])

        if queued_set_light_data[ip]["timestamp"] is None:
            logging.debug("First set_light call for %s, invoking now", ip)
            callback()
        else:
            now = event_loop.time() * 1000
            delay = (minimum_msec_between_commands_for_ip(ip))
            last_time = queued_set_light_data[ip]["timestamp"]

            if now - last_time > delay:
                logging.debug(
                    "last set_light call for %s was more than %f ms ago (was %f ms), invoking now", ip, delay, now - last_time)
                callback()
            elif queued_set_light_data[ip]["timer"] is None:
                logging.debug(
                    "last set_light call for %s was %f ms ago, invoking in %f", ip, now - last_time, delay)
                queued_set_light_data[ip]["timer"] = event_loop.call_soon_threadsafe(
                    partial(event_loop.call_at, (now + delay) / 1000, callback))


def do_set_light_data(ip, light, data):
    try:
        data["timestamp"] = event_loop.time() * 1000
        commands(ip, light, data["data"])
        data["data"] = {}
    finally:
        data["timer"] = None


def commands(ip, light, data):
    try:
        connection = get_or_create_connection(ip)
    except Exception as ex:
        raise

    payload = convert_to_payload(data, light)

    msg = ""
    for (method, params) in payload.items():
        msg += json.dumps(
            {"id": 0, "method": method, "params": params}) + "\r\n"

    try:
        connection.send(msg.encode())
    except Exception as ex:
        logging.exception("Command send exception")
        if connection.mode == 'music':
            logging.info("Retrying command on another connection")
            return commands(ip, light, data)
        else:
            raise


def convert_to_payload(data, light):
    payload = {}

    if "on" in data:
        if data["on"]:
            payload["set_power"] = ["on", *TRANSITION]
        else:
            payload["set_power"] = ["off", *TRANSITION]

    if "bri" in data:
        payload["set_bright"] = [
            int(data["bri"] / 2.55) + 1, *TRANSITION]

    if "ct" in data:
        payload["set_ct_abx"] = [
            int(1000000 / data["ct"]), *TRANSITION]

    if "hue" in data and "sat" in data:
        payload["set_hsv"] = [
            int(data["hue"] / 182), int(data["sat"] / 2.54), *TRANSITION]
    elif "hue" in data and "sat" not in data:
        payload["set_hsv"] = [
            int(data["hue"] / 182), int(light["state"]["sat"] / 2.54), *TRANSITION]
    elif "hue" in data and "sat" not in data:
        payload["set_hsv"] = [
            int(light["state"]["hue"] / 182), int(data["sat"] / 2.54), *TRANSITION]

    if "rgb" in data:
        color = data["rgb"]
        payload["set_rgb"] = [
            (color[0] * 65536) + (color[1] * 256) + color[2], *TRANSITION]

    elif "xy" in data:  # prefer rgb if possible over xy
        if "bri" in data:
            bri = data["bri"]
        else:
            bri = light["state"]["bri"]

        color = convert_xy(data["xy"][0], data["xy"][1], bri)
        # according to docs, yeelight needs this to set rgb. its r * 65536 + g * 256 + b
        payload["set_rgb"] = [
            (color[0] * 65536) + (color[1] * 256) + color[2], *TRANSITION]

    if "alert" in data and data["alert"] != "none":
        payload["start_cf"] = [
            4, 0, "1000, 2, 5500, 100, 1000, 2, 5500, 1, 1000, 2, 5500, 100, 1000, 2, 5500, 1"]

    return payload


def set_light(ip, light, data):
    method = 'TCP'
    payload = {}
    transitiontime = 400
    if "transitiontime" in data:
        transitiontime = data["transitiontime"] * 10

    queue_set_light_data(ip, light, data)


def get_light_state(ip, light):
    state = {}
    try:
        connection = get_or_create_connection(ip, music=False)
    except Exception as ex:
        raise

    data = connection.invoke_command("get_prop", "power", "bright").result(3)
    light_data = data["result"]

    if light_data[0] == "on":  # powerstate
        state['on'] = True
    else:
        state['on'] = False
    state["bri"] = int(int(light_data[1]) * 2.54)

    data = connection.invoke_command("get_prop", "color_mode").result(3)
    if data["result"][0] == "1":  # rgb mode
        data = connection.invoke_command("get_prop", "rgb").result(3)
        hue_data = data["result"]
        hex_rgb = "%6x" % int(hue_data[0])
        r = hex_rgb[: 2]
        if r == "  ":
            r = "00"
        g = hex_rgb[3: 4]
        if g == "  ":
            g = "00"
        b = hex_rgb[-2:]
        if b == "  ":
            b = "00"
        state["xy"] = convert_rgb_xy(int(r, 16), int(g, 16), int(b, 16))
        state["colormode"] = "xy"
    elif data["result"][0] == "2":  # ct mode
        data = connection.invoke_command("get_prop", "ct").result(3)
        state["ct"] = int(
            1000000 / int(data["result"][0]))
        state["colormode"] = "ct"

    elif data["result"][0] == "3":  # ct mode
        data = connection.invoke_command("get_prop", "hue", "sat").result(3)
        hue_data = data["result"]
        state["hue"] = int(hue_data[0] * 182)
        state["sat"] = int(int(hue_data[1]) * 2.54)
        state["colormode"] = "hs"
    return state
